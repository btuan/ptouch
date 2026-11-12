"""ptlabel command line. Every command that takes a spec accepts a Job or a Label
as JSON (file path or "-" for stdin), so agents can pipe specs straight in.

    ptlabel schema Job                     # JSON Schema to design against
    ptlabel render spec.json -o out.png    # preview, no printer needed
    ptlabel print spec.json                # print (checks loaded tape first)
    ptlabel text "HELLO" --print
    ptlabel template list
    ptlabel template render asset-tag --var id=A-1 --var location="Lab" -o tag.png
    ptlabel template print asset-tag --rows rows.csv
    ptlabel status
    ptlabel serve                          # HTTP API on 127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .printer import Printer, PrinterError, compile_job, label_length_mm
from .render import RenderError, preview
from .spec import Job, Label, schemas
from .templates import TemplateStore


def load_job(src: str, tape: int | None = None) -> Job:
    text = sys.stdin.read() if src == "-" else Path(src).read_text()
    data = json.loads(text)
    job = Job.model_validate(data) if "labels" in data else Job(labels=[Label.model_validate(data)])
    if tape:
        job = job.model_copy(update={"tape_mm": tape})
    return job


def resolve_tape(job: Job, printer: Printer | None, fallback: int = 12) -> int:
    """Spec's tape width, else the loaded tape (SNMP), else `fallback` for previews."""
    loaded = None
    if job.tape_mm is None and all(l.tape_mm is None for l in job.labels) and printer:
        try:
            loaded = printer.status().tape_mm
        except PrinterError:
            pass
    return job.resolved_tape(loaded or fallback)


def do_render(job: Job, out: str, printer: Printer | None) -> dict:
    tape = resolve_tape(job, printer)
    data, images = compile_job(job, tape)
    preview(images, tape, job.hires, job.separator, job.margin_mm).save(out)
    return {"preview": out, "tape_mm": tape, "labels": len(images), "bytes": len(data),
            "length_mm": round(label_length_mm(images, job), 1)}


def do_print(job: Job, printer: Printer, dry_run: bool, bin_out: str | None) -> dict:
    if dry_run:
        tape = resolve_tape(job, printer)
        data, images = compile_job(job, tape)
        if bin_out:
            Path(bin_out).write_bytes(data)
        return {"dry_run": True, "tape_mm": tape, "labels": len(images), "bytes": len(data),
                "length_mm": round(label_length_mm(images, job), 1)}
    return printer.print(job)


def _vars(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k] = v
    return out


def _rows(path: str | None, single: dict) -> list[dict]:
    if not path:
        return [single]
    text = Path(path).read_text()
    rows = json.loads(text) if path.endswith(".json") else list(csv.DictReader(text.splitlines()))
    return [{**single, **r} for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ptlabel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--printer", default=None, help="printer host (env PTLABEL_PRINTER)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p = sub.add_parser("schema"); p.add_argument("which", nargs="?", choices=["Label", "Job", "Template"])
    p = sub.add_parser("render"); p.add_argument("spec"); p.add_argument("-o", "--out", default="label.png")
    p.add_argument("--tape", type=int)
    p = sub.add_parser("print"); p.add_argument("spec"); p.add_argument("--tape", type=int)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--bin")
    p = sub.add_parser("text"); p.add_argument("text"); p.add_argument("--font", default="sans")
    p.add_argument("--tape", type=int); p.add_argument("--print", action="store_true")
    p.add_argument("-o", "--out", default="label.png")
    p = sub.add_parser("decode"); p.add_argument("bin"); p.add_argument("-o", "--out", default="decoded.png")
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    t = sub.add_parser("template").add_subparsers(dest="tcmd", required=True)
    t.add_parser("list")
    p = t.add_parser("show"); p.add_argument("name")
    for name in ("render", "print"):
        p = t.add_parser(name); p.add_argument("name")
        p.add_argument("--var", action="append", help="name=value (repeatable)")
        p.add_argument("--rows", help="CSV or JSON list of variable rows -> one label each")
        p.add_argument("--tape", type=int)
        if name == "render":
            p.add_argument("-o", "--out", default="label.png")
        else:
            p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args(argv)
    printer = Printer(a.printer) if a.printer else Printer()
    try:
        result = _run(a, printer)
    except (ValidationError, RenderError, PrinterError, ValueError, KeyError, OSError) as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)}), file=sys.stderr)
        sys.exit(1)
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def _run(a, printer: Printer):
    if a.cmd == "status":
        return printer.status().as_dict()
    if a.cmd == "schema":
        s = schemas()
        return s[a.which] if a.which else s
    if a.cmd == "render":
        return do_render(load_job(a.spec, a.tape), a.out, printer)
    if a.cmd == "print":
        return do_print(load_job(a.spec, a.tape), printer, a.dry_run, a.bin)
    if a.cmd == "text":
        job = Job(tape_mm=a.tape, labels=[Label(items=[{"type": "text", "text": a.text.replace("\\n", "\n"),
                                                         "font": a.font}])])
        return printer.print(job) if a.print else do_render(job, a.out, printer)
    if a.cmd == "decode":
        from .decode import decode
        pages = decode(Path(a.bin).read_bytes())
        imgs = [p.image() for p in pages]
        s0 = pages[0].settings
        preview(imgs, s0["tape_mm"], s0.get("hires", False)).save(a.out)
        return {"pages": [p.settings | {"end": p.end} for p in pages], "preview": a.out}
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("ptlabel.api:app", host=a.host, port=a.port)
        return None
    if a.cmd == "template":
        store = TemplateStore()
        if a.tcmd == "list":
            return [{"name": t.name, "description": t.description, "variables": list(t.variables)}
                    for t in store.list()]
        tpl = store.get(a.name)
        if a.tcmd == "show":
            return tpl.model_dump()
        job = tpl.job(_rows(a.rows, _vars(a.var)), tape_mm=a.tape)
        if a.tcmd == "render":
            return do_render(job, a.out, printer)
        return do_print(job, printer, a.dry_run, None)


if __name__ == "__main__":
    main()
