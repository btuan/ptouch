"""Generative label design: Claude turns a description into a Job (and optionally a Template).

The agent loop is small and runs here, not on the model side: Claude writes a Job,
calls `render_preview` to see the exact raster image the printer would get, fixes what
looks wrong, and finishes with `submit_design`. Nothing is printed; the UI shows the
result and the person decides.

Needs ANTHROPIC_API_KEY. PTLABEL_MODEL picks the model (default claude-opus-5-5).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

import anthropic
from pydantic import ValidationError

from .printer import compile_job, label_length_mm
from .render import FONTS, RenderError, label_spans_mm, preview
from .spec import TAPE_WIDTHS, Job, schemas
from .templates import Template

log = logging.getLogger(__name__)
MODEL = os.environ.get("PTLABEL_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PTLABEL_EFFORT", "medium")
MAX_TURNS = int(os.environ.get("PTLABEL_DESIGN_TURNS", "10"))
_slots = threading.BoundedSemaphore(int(os.environ.get("PTLABEL_DESIGN_CONCURRENCY", "2")))

TOOLS = [
    {
        "name": "render_preview",
        "description": (
            "Render a Job spec exactly as the printer would print it. Returns the preview image "
            "(white tape, black print, dashed lines are half-cuts) plus length in mm, or the "
            "validation/render error to fix. Call this before submit_design and look at the image."),
        "input_schema": {"type": "object", "properties": {
            "job": {"type": "object", "description": "A Job spec (see the schema in the system prompt)."}},
            "required": ["job"]},
    },
    {
        "name": "submit_design",
        "description": (
            "Finish: hand the design to the person. `job` is what will be shown and can be printed. "
            "Include `template` only when the person asked for something reusable, a series, or a "
            "batch (e.g. 'asset tags for every machine'); then `job` should be the template filled "
            "with its example values."),
        "input_schema": {"type": "object", "properties": {
            "job": {"type": "object"},
            "summary": {"type": "string", "description": "One or two sentences for the person: what you made and any trade-off."},
            "template": {"type": "object", "description": "Optional Template spec (name, description, variables, label)."}},
            "required": ["job", "summary"]},
    },
]


def _system() -> str:
    s = schemas()
    return f"""You design labels for a Brother PT-P950NW label printer (360 dpi, thermal, black on white TZe tape).

A label is a row of items laid out along the tape; the tape width is the label's height. A Job is one
or more labels printed as a single strip. Write specs as JSON matching these schemas exactly (unknown
fields are rejected):

Job schema:
{json.dumps(s["Job"], separators=(",", ":"))}

Template schema (strings in `label` may contain {{{{variable}}}} placeholders):
{json.dumps(s["Template"], separators=(",", ":"))}

Facts that matter:
- Tape widths: {list(TAPE_WIDTHS)} mm. The printable height is smaller than the tape (12 mm tape prints ~10.6 mm).
- Fonts: {sorted(FONTS)}. Text auto-fits the tape height unless size_mm is set; multi-line text shares the height.
- Text under ~1.8 mm tall is hard to read. QR codes need modules >= 0.2 mm; long data on narrow tape may not fit
  (use datamatrix, shorter data, or wider tape). Barcode symbologies validate their data (ean13 = 12-13 digits).
- Tape costs money: keep labels as short as the content allows, and put several labels in one job separated
  by half_cut rather than separate jobs. Don't add copies unless asked.
- Use the tape width you're told is loaded unless the person asks otherwise.

How to work:
1. Draft a Job. If the person gave you a current design, edit it rather than starting over unless they ask.
2. Call render_preview and look at the image critically: legibility, balance, wasted tape, clipping.
3. Fix and re-render if needed (a couple of rounds is usually enough).
4. Call submit_design with the final job and a short summary. Do not write the spec in plain text."""


def _render(job: Job, tape: int) -> tuple[dict, bytes]:
    data, images = compile_job(job, tape)
    buf = io.BytesIO()
    preview(images, tape, job.hires, job.separator, job.margin_mm).save(buf, "PNG")
    meta = {"tape_mm": tape, "labels": len(images),
            "length_mm": round(label_length_mm(images, job), 1),
            "label_spans_mm": label_spans_mm(images, job.hires, job.margin_mm)}
    return meta, buf.getvalue()


def _validate(spec: Any, loaded: int) -> tuple[Job | None, str | None]:
    if not isinstance(spec, dict):
        return None, "job must be a JSON object"
    try:
        job = Job.model_validate(spec)
        job.resolved_tape(loaded)
        return job, None
    except (ValidationError, ValueError) as e:
        return None, f"Invalid Job: {e}"


def _check_template(spec: Any) -> tuple[dict | None, str | None]:
    if spec is None:
        return None, None
    try:
        t = Template.model_validate(spec)
        t.check()
        return t.model_dump(exclude_none=True), None
    except (ValidationError, ValueError, RenderError) as e:
        return None, f"Invalid template: {e}"


def design(prompt: str, current: dict | None, tape_mm: int,
           client: anthropic.Anthropic | None = None) -> Iterator[dict]:
    """Yield progress events, ending with {"type": "done", ...} or {"type": "error", ...}."""
    if not _slots.acquire(blocking=False):
        yield {"type": "error", "msg": "The designer is busy with other requests. Try again in a moment."}
        return
    try:
        yield from _loop(prompt, current, tape_mm, client or anthropic.Anthropic())
    except anthropic.AuthenticationError:
        yield {"type": "error", "msg": "The server's Claude API key was rejected."}
    except anthropic.RateLimitError:
        yield {"type": "error", "msg": "Claude API rate limit reached. Try again shortly."}
    except anthropic.APIConnectionError:
        yield {"type": "error", "msg": "Couldn't reach the Claude API from the server."}
    except anthropic.APIStatusError as e:
        yield {"type": "error", "msg": f"Claude API error {e.status_code}: {e.message}"}
    except Exception as e:  # never drop the stream silently
        log.exception("design failed")
        yield {"type": "error", "msg": f"The designer hit an internal error ({type(e).__name__}). Try again or rephrase."}
    finally:
        _slots.release()


def _loop(prompt: str, current: dict | None, tape_mm: int, client: anthropic.Anthropic) -> Iterator[dict]:
    text = f"Loaded tape: {tape_mm} mm.\n\nRequest: {prompt.strip()}"
    if current:
        text += "\n\nCurrent design (edit this if the request is a change):\n" + json.dumps(current)
    messages: list[dict] = [{"role": "user", "content": text}]
    system = [{"type": "text", "text": _system(), "cache_control": {"type": "ephemeral"}}]
    renders = 0
    yield {"type": "status", "text": "Thinking about the design…"}

    for _ in range(MAX_TURNS):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=system, tools=TOOLS, messages=messages,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            yield {"type": "error", "msg": "Claude declined this request."}
            return
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": "Use render_preview, then submit_design to finish."})
            continue

        results = []
        for use in uses:
            spec = use.input.get("job") if isinstance(use.input, dict) else None
            job, err = _validate(spec, tape_mm)
            if use.name == "submit_design":
                tpl, terr = _check_template(use.input.get("template"))
                err = err or terr
                if not err:
                    try:
                        meta, png = _render(job, job.resolved_tape(tape_mm))
                    except RenderError as e:
                        err = f"Render error at {e.loc}: {e}"
                    except Exception as e:
                        log.exception("render failed")
                        err = f"Renderer crashed ({type(e).__name__}: {e}); try a different layout."
                if not err:
                    yield {"type": "done", "job": job.model_dump(mode="json", exclude_unset=True),
                           "summary": str(use.input.get("summary", "")), "template": tpl, "renders": renders,
                           **meta, "preview_png_base64": base64.b64encode(png).decode()}
                    return
                results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                "content": err + " Fix it and call submit_design again."})
            elif use.name == "render_preview":
                if not err:
                    try:
                        meta, png = _render(job, job.resolved_tape(tape_mm))
                    except RenderError as e:
                        err = f"Render error at {e.loc}: {e}"
                    except Exception as e:
                        log.exception("render failed")
                        err = f"Renderer crashed ({type(e).__name__}: {e}); try a different layout."
                if err:
                    yield {"type": "status", "text": "Fixing a problem with the spec…"}
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True, "content": err})
                    continue
                renders += 1
                b64 = base64.b64encode(png).decode()
                yield {"type": "preview", "n": renders, "preview_png_base64": b64, **meta}
                yield {"type": "status", "text": f"Checking draft {renders}…"}
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": [
                    {"type": "text", "text": json.dumps(meta)},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}]})
            else:
                results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                "content": f"unknown tool {use.name}"})
        messages.append({"role": "user", "content": results})

    yield {"type": "error", "msg": "The designer ran out of attempts without finishing. Try a simpler request."}


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
