"""HTTP API. Run with `ptlabel serve` (binds 127.0.0.1:8765 by default).

Designed for agents and the future web UI: everything is JSON in, JSON or PNG out,
and GET /schema returns the JSON Schemas to design labels against.

Set PTLABEL_TOKEN to require `Authorization: Bearer <token>` on every request.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

import json

from . import __doc__ as pkg_doc
from . import design as designer
from .cli import resolve_tape
import threading

from . import device, fonts
from . import discover as discovery
from .printer import (Printer, PrinterError, build_registry, compile_job, cuts_between, default_printers,
                      label_length_mm, pick_default, separator)
from .design_tools import Lookups
from .library import Library, NotFound, job_thumb, open_default, print_and_record
from .render import FONTS, RenderError, label_boxes, label_spans_mm, preview
from .spec import TAPE_WIDTHS, Job, Label, schemas
from .templates import Template, TemplateStore


def _auth(authorization: str | None = Header(None)):
    token = os.environ.get("PTLABEL_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "missing or bad bearer token")


app = FastAPI(title="ptlabel", description=pkg_doc, version="0.1.0")
api = APIRouter(dependencies=[Depends(_auth)])
WEB = Path(__file__).parent / "web"
# PTLABEL_PRINTER + USB printers now; a network sweep runs in the background at startup
# (PTLABEL_DISCOVER=0 to skip) and on POST /printers/discover.
printers: dict[str, Printer] = default_printers()
other_devices: list[dict] = []          # Brother devices found that ptlabel can't drive
_scan_lock = threading.Lock()


def rescan(network: bool = True) -> None:
    global printers, other_devices
    with _scan_lock:
        found = discovery.usb() if os.environ.get("PTLABEL_USB", "1") != "0" else []
        if network:
            found += discovery.network()
        printers = build_registry(found, keep=printers)
        other_devices = [f.as_dict() for f in found if not f.supported]


@app.on_event("startup")
def _discover_in_background():
    if os.environ.get("PTLABEL_DISCOVER", "1") != "0":
        threading.Thread(target=rescan, daemon=True).start()


def pick(key: str | None) -> Printer | None:
    """A printer by key; None key = the default printer (None if there are no printers)."""
    if key is None:
        return pick_default(printers)
    if key not in printers:
        raise HTTPException(404, f"no printer {key!r}; have {sorted(printers)}")
    return printers[key]


def _need(p: Printer | None) -> Printer:
    if p is None:
        raise HTTPException(503, "no printer: plug one in over USB, set PTLABEL_PRINTER, "
                                 "or POST /printers/discover")
    return p


store = TemplateStore(os.environ.get("PTLABEL_TEMPLATES") or TemplateStore().root)
lib: Library | None = open_default()      # saved labels + print history (PTLABEL_DB)


def _lib() -> Library:
    if lib is None:
        raise HTTPException(503, "the label library is turned off (PTLABEL_DB=off)")
    return lib


def _thumb_for(job: Job, p: Printer | None, tape_mm: int | None) -> bytes | None:
    """Thumbnail without asking the printer anything: spec tape, else `tape_mm`, else 12."""
    try:
        tape = job.resolved_tape(tape_mm or 12)
    except ValueError:
        tape = 12
    return job_thumb(job, tape, p.model if p else device.P950NW)


def as_job(spec: dict[str, Any]) -> Job:
    try:
        return Job.model_validate(spec) if "labels" in spec else Job(labels=[Label.model_validate(spec)])
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False)) from e


def _render_http(e: RenderError) -> HTTPException:
    """Same shape as pydantic validation errors, so clients handle one format."""
    return HTTPException(422, [{"loc": ["body", *e.loc], "msg": str(e), "type": "render_error"}])


def _render(job: Job, fmt: str, frame: bool = True, p: Printer | None = None, collapse: bool = False):
    """p=None renders for the default model (PT-P950NW) without asking any printer."""
    m = p.model if p else device.P950NW
    try:
        tape = resolve_tape(job, p)
        data, images = compile_job(job, tape, m)
    except RenderError as e:
        raise _render_http(e) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    buf = io.BytesIO()
    all_cuts = cuts_between(job, m)
    n = len(job.labels)
    # one set stands for all only if every set is cut identically (full_cut_every may not divide n)
    periodic = job.copies > 1 and all(c == all_cuts[i % n] for i, c in enumerate(all_cuts))
    shown = images[:n] if collapse and periodic else images
    cuts = all_cuts[:max(0, len(shown) - 1)]
    # whole job shown: its end is the job's end cut; one set shown: the cut between copies
    end_cut = (not job.chain) if shown is images else all_cuts[n - 1] == "full_cut"
    preview(shown, tape, job.hires, cuts, job.margin_mm, frame=frame, model=m, end_cut=end_cut).save(buf, "PNG")
    meta = {"printer": p.key if p else None, "tape_mm": tape, "labels": len(images), "bytes": len(data),
            "copies": job.copies, "preview_copies": 1 if shown is not images else job.copies,
            "label_spans_mm": label_spans_mm(shown, job.hires, job.margin_mm, m.dpi),
            "label_boxes": label_boxes(shown, tape, job.hires, cuts, job.margin_mm, m),
            "cuts": cuts, "pieces": 1 + all_cuts.count("full_cut"),
            "copy_cut": all_cuts[n - 1] if job.copies > 1 else None,
            "length_mm": round(label_length_mm(images, job, m.dpi), 1)}
    if fmt == "json":
        return meta | {"preview_png_base64": base64.b64encode(buf.getvalue()).decode()}
    return Response(buf.getvalue(), media_type="image/png",
                    headers={f"X-Label-{k.replace('_', '-')}": str(v) for k, v in meta.items()})


def _print(job: Job, dry_run: bool, p: Printer | None = None, **record):
    """record: label_id / template / vars / reprint_of / source for the print history."""
    if dry_run:
        r = _render(job, "json", p=p)
        r.pop("preview_png_base64")
        return r | {"dry_run": True}
    p = _need(p)
    record.setdefault("source", "api")
    try:
        return print_and_record(lib, p, job, **record)
    except RenderError as e:
        raise _render_http(e) from e
    except PrinterError as e:
        raise HTTPException(503, str(e)) from e


# ------------------------------------------------------------------ routes

@api.get("/schema")
def get_schema():
    """JSON Schemas for Label, Job and Template."""
    return schemas()


@api.get("/printers")
def list_printers():
    """Printers this server can use, with what each supports. Status is per printer."""
    return [p.describe() for p in printers.values()]


@api.post("/printers/discover")
def discover_printers():
    """Look for printers on USB and the local network (~2 s); keys of known printers stay the same."""
    rescan(network=True)
    return {"printers": [p.describe() for p in printers.values()], "other_devices": other_devices}


@api.get("/printer/status")
def get_status(printer_key: str | None = Query(None, alias="printer"),
               fresh: bool = Query(False, description="Ask the printer now instead of using a recent answer.")):
    p = _need(pick(printer_key))
    try:
        return p.status(max_age=0 if fresh else None).as_dict() | {"printer": p.key, "connection": p.where}
    except PrinterError as e:
        raise HTTPException(503, str(e)) from e


@api.post("/render", responses={200: {"content": {"image/png": {}}}})
def render(spec: dict[str, Any], format: str = Query("png", pattern="^(png|json)$"),
           frame: bool = True, printer_key: str | None = Query(None, alias="printer"),
           collapse_copies: bool = Query(False, description="Preview one set of labels when copies > 1 "
                                                             "(labels/length still count every copy).")):
    """Render a Job or Label to a preview (PNG, or JSON with base64 PNG + metadata)."""
    return _render(as_job(spec), format, frame, pick(printer_key), collapse_copies)


@api.post("/print")
def print_(spec: dict[str, Any], dry_run: bool = False,
           printer_key: str | None = Query(None, alias="printer"),
           label_id: int | None = Query(None, description="Saved label this came from (for history)."),
           reprint_of: int | None = Query(None, description="History entry this reprints (for history).")):
    """Print a Job or Label. Checks the loaded tape matches first. Logged to GET /history."""
    return _print(as_job(spec), dry_run, pick(printer_key), label_id=label_id, reprint_of=reprint_of,
                  source="reprint" if reprint_of else "api")


class Fill(BaseModel):
    vars: dict[str, Any] = Field(default_factory=dict, description="Values for one label.")
    rows: list[dict[str, Any]] | None = Field(None, description="One label per row (batch).")
    job: dict[str, Any] = Field(default_factory=dict, description="Job options, e.g. tape_mm, copies.")

    def to_job(self, t: Template) -> Job:
        rows = [{**self.vars, **r} for r in self.rows] if self.rows else [self.vars]
        try:
            return t.job(rows, **self.job)
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, str(e)) from e


def _get(name: str) -> Template:
    try:
        return store.get(name)
    except KeyError:
        raise HTTPException(404, f"no template {name!r}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@api.get("/templates")
def list_templates():
    return [{"name": t.name, "description": t.description, "variables": t.variables}
            for t in store.list()]


@api.get("/templates/{name}")
def get_template(name: str):
    return _get(name)


@api.put("/templates/{name}")
def put_template(name: str, t: Template):
    if t.name != name:
        raise HTTPException(400, "name in body must match URL")
    try:
        return store.put(t)
    except (ValidationError, ValueError, RenderError) as e:
        raise HTTPException(422, str(e)) from e


@api.delete("/templates/{name}", status_code=204)
def delete_template(name: str):
    try:
        store.delete(name)
    except KeyError:
        raise HTTPException(404, f"no template {name!r}")


@api.post("/templates/{name}/render", responses={200: {"content": {"image/png": {}}}})
def render_template(name: str, fill: Fill, format: str = Query("png", pattern="^(png|json)$"),
                    frame: bool = True, printer_key: str | None = Query(None, alias="printer")):
    return _render(fill.to_job(_get(name)), format, frame, pick(printer_key))


@api.post("/templates/{name}/print")
def print_template(name: str, fill: Fill, dry_run: bool = False,
                   printer_key: str | None = Query(None, alias="printer")):
    return _print(fill.to_job(_get(name)), dry_run, pick(printer_key), template=name,
                  vars=fill.rows if fill.rows else fill.vars)


# ------------------------------------------------------------------ library: saved labels

class SaveLabel(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    job: dict[str, Any]
    origin: str | None = Field(None, max_length=100, description="manual | claude | template:<name> | history")


class UpdateLabel(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    job: dict[str, Any] | None = None


def _png(data: bytes | None) -> Response:
    if not data:
        raise HTTPException(404, "no thumbnail")
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@api.get("/labels")
def list_labels(q: str | None = None, limit: int = Query(100, le=500), offset: int = 0):
    """Saved labels, newest first. `q` searches names and label text."""
    return _lib().labels(q, limit, offset)


@api.post("/labels", status_code=201)
def save_label(body: SaveLabel, printer_key: str | None = Query(None, alias="printer"),
               tape_mm: int | None = Query(None, description="Tape to draw the thumbnail on.")):
    job = as_job(body.job)
    return _lib().save(body.name, job.model_dump(mode="json", exclude_none=True), body.origin,
                       _thumb_for(job, pick(printer_key), tape_mm))


@api.get("/labels/{label_id}")
def get_label(label_id: int):
    try:
        return _lib().get(label_id)
    except NotFound:
        raise HTTPException(404, f"no saved label {label_id}")


@api.put("/labels/{label_id}")
def update_label(label_id: int, body: UpdateLabel, printer_key: str | None = Query(None, alias="printer"),
                 tape_mm: int | None = None):
    job = as_job(body.job) if body.job is not None else None
    try:
        return _lib().update(label_id, body.name,
                             job.model_dump(mode="json", exclude_none=True) if job else None,
                             _thumb_for(job, pick(printer_key), tape_mm) if job else None)
    except NotFound:
        raise HTTPException(404, f"no saved label {label_id}")


@api.delete("/labels/{label_id}", status_code=204)
def delete_label(label_id: int):
    try:
        _lib().delete(label_id)
    except NotFound:
        raise HTTPException(404, f"no saved label {label_id}")


@api.post("/labels/{label_id}/restore")
def restore_label(label_id: int):
    """Undo a delete (deleted labels are kept 30 days)."""
    try:
        return _lib().restore(label_id)
    except NotFound:
        raise HTTPException(404, f"no deleted label {label_id}")


@api.get("/labels/{label_id}/thumb.png", include_in_schema=False)
def label_thumb(label_id: int):
    try:
        return _png(_lib().thumb("labels", label_id))
    except NotFound:
        raise HTTPException(404, f"no saved label {label_id}")


# ------------------------------------------------------------------ library: print history

@api.get("/history")
def history(q: str | None = None, printer_key: str | None = Query(None, alias="printer"),
            ok: bool | None = None, limit: int = Query(50, le=500), offset: int = 0):
    """Every print (newest first): what was sent, to which printer, tape used, success or error."""
    return _lib().history(q, limit, offset, printer_key, ok)


@api.get("/history/stats")
def history_stats():
    """Totals: prints, labels, tape used (overall and per printer/tape width)."""
    return _lib().stats()


@api.get("/history/{print_id}")
def history_entry(print_id: int):
    try:
        return _lib().print_entry(print_id)
    except NotFound:
        raise HTTPException(404, f"no print {print_id}")


@api.get("/history/{print_id}/thumb.png", include_in_schema=False)
def history_thumb(print_id: int):
    try:
        return _png(_lib().thumb("prints", print_id))
    except NotFound:
        raise HTTPException(404, f"no print {print_id}")


@api.post("/history/{print_id}/reprint")
def reprint(print_id: int, dry_run: bool = False, printer_key: str | None = Query(None, alias="printer")):
    """Print a past job again, on `printer` (default: the printer it was printed on, if still here)."""
    e = history_entry(print_id)
    key = printer_key or (e["printer"] if e["printer"] in printers else None)
    return _print(as_job(e["job"]), dry_run, pick(key), source="reprint", reprint_of=print_id,
                  label_id=e["label_id"], template=e["template"], vars=e["vars"])


@api.get("/meta")
def meta():
    """Choices for building UIs: fonts, tape widths, item types."""
    return {"fonts": fonts.NAMES, "font_aliases": fonts.ALIASES, "tape_widths": list(TAPE_WIDTHS),
            "item_types": ["text", "qr", "datamatrix", "barcode", "image", "spacer", "rule"],
            "printer": (d.key if (d := pick_default(printers)) else None),
            "printers": [p.describe() for p in printers.values()],
            "design": designer.available(), "design_model": designer.MODEL}


class DesignRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    job: dict[str, Any] | None = Field(None, description="Current design to edit, if any.")
    tape_mm: int | None = Field(None, description="Tape to design for; default: loaded tape.")
    printer: str | None = Field(None, description="Printer key to design for (resolution, tapes, cuts).")


@api.post("/design")
def design(req: DesignRequest):
    """Describe a label; Claude drafts, renders, checks and returns a Job.

    Server-sent events, one JSON object per `data:` line: `status`, `preview` (each draft),
    then `done` (job, summary, optional template, preview) or `error`. Nothing is printed.
    """
    if not designer.available():
        raise HTTPException(503, "generative design is not configured (no ANTHROPIC_API_KEY)")
    p = pick(req.printer)
    tape = req.tape_mm
    if tape is None and p is not None:
        try:
            tape = p.status().tape_mm
        except PrinterError:
            pass
    tape = tape or 12

    def events():
        for ev in designer.design(req.prompt, req.job, tape, model=p.model if p else device.P950NW,
                                  lookups=Lookups(lib, store)):
            yield f"data: {json.dumps(ev)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.include_router(api)


@app.middleware("http")
async def _no_stale_ui(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")
