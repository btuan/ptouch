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
from .printer import Printer, PrinterError, compile_job, label_length_mm
from .render import FONTS, RenderError, label_spans_mm, preview
from .spec import TAPE_WIDTHS, Job, Label, schemas
from .templates import Template, TemplateStore


def _auth(authorization: str | None = Header(None)):
    token = os.environ.get("PTLABEL_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "missing or bad bearer token")


app = FastAPI(title="ptlabel", description=pkg_doc, version="0.1.0")
api = APIRouter(dependencies=[Depends(_auth)])
WEB = Path(__file__).parent / "web"
printer = Printer()
store = TemplateStore(os.environ.get("PTLABEL_TEMPLATES") or TemplateStore().root)


def as_job(spec: dict[str, Any]) -> Job:
    try:
        return Job.model_validate(spec) if "labels" in spec else Job(labels=[Label.model_validate(spec)])
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False)) from e


def _render_http(e: RenderError) -> HTTPException:
    """Same shape as pydantic validation errors, so clients handle one format."""
    return HTTPException(422, [{"loc": ["body", *e.loc], "msg": str(e), "type": "render_error"}])


def _render(job: Job, fmt: str, frame: bool = True):
    try:
        tape = resolve_tape(job, printer)
        data, images = compile_job(job, tape)
    except RenderError as e:
        raise _render_http(e) from e
    buf = io.BytesIO()
    preview(images, tape, job.hires, job.separator, job.margin_mm, frame=frame).save(buf, "PNG")
    meta = {"tape_mm": tape, "labels": len(images), "bytes": len(data),
            "label_spans_mm": label_spans_mm(images, job.hires, job.margin_mm),
            "length_mm": round(label_length_mm(images, job), 1)}
    if fmt == "json":
        return meta | {"preview_png_base64": base64.b64encode(buf.getvalue()).decode()}
    return Response(buf.getvalue(), media_type="image/png",
                    headers={f"X-Label-{k.replace('_', '-')}": str(v) for k, v in meta.items()})


def _print(job: Job, dry_run: bool):
    if dry_run:
        r = _render(job, "json")
        r.pop("preview_png_base64")
        return r | {"dry_run": True}
    try:
        return printer.print(job)
    except RenderError as e:
        raise _render_http(e) from e
    except PrinterError as e:
        raise HTTPException(503, str(e)) from e


# ------------------------------------------------------------------ routes

@api.get("/schema")
def get_schema():
    """JSON Schemas for Label, Job and Template."""
    return schemas()


@api.get("/printer/status")
def get_status():
    try:
        return printer.status().as_dict() | {"host": printer.host}
    except PrinterError as e:
        raise HTTPException(503, str(e)) from e


@api.post("/render", responses={200: {"content": {"image/png": {}}}})
def render(spec: dict[str, Any], format: str = Query("png", pattern="^(png|json)$"),
           frame: bool = True):
    """Render a Job or Label to a preview (PNG, or JSON with base64 PNG + metadata)."""
    return _render(as_job(spec), format, frame)


@api.post("/print")
def print_(spec: dict[str, Any], dry_run: bool = False):
    """Print a Job or Label. Checks the loaded tape matches first."""
    return _print(as_job(spec), dry_run)


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
                    frame: bool = True):
    return _render(fill.to_job(_get(name)), format, frame)


@api.post("/templates/{name}/print")
def print_template(name: str, fill: Fill, dry_run: bool = False):
    return _print(fill.to_job(_get(name)), dry_run)


@api.get("/meta")
def meta():
    """Choices for building UIs: fonts, tape widths, item types."""
    return {"fonts": sorted(FONTS), "tape_widths": list(TAPE_WIDTHS),
            "item_types": ["text", "qr", "datamatrix", "barcode", "image", "spacer", "rule"],
            "printer": printer.host, "design": designer.available(), "design_model": designer.MODEL}


class DesignRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    job: dict[str, Any] | None = Field(None, description="Current design to edit, if any.")
    tape_mm: int | None = Field(None, description="Tape to design for; default: loaded tape.")


@api.post("/design")
def design(req: DesignRequest):
    """Describe a label; Claude drafts, renders, checks and returns a Job.

    Server-sent events, one JSON object per `data:` line: `status`, `preview` (each draft),
    then `done` (job, summary, optional template, preview) or `error`. Nothing is printed.
    """
    if not designer.available():
        raise HTTPException(503, "generative design is not configured (no ANTHROPIC_API_KEY)")
    tape = req.tape_mm
    if tape is None:
        try:
            tape = printer.status().tape_mm
        except PrinterError:
            pass
    tape = tape or 12

    def events():
        for ev in designer.design(req.prompt, req.job, tape):
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
