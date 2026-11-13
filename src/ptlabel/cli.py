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

from .device import P950NW
from .printer import (Printer, PrinterError, compile_job, default_printers, get_printer,
                      cuts_between, label_length_mm)
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
    model = printer.model if printer else P950NW
    data, images = compile_job(job, tape, model)
    preview(images, tape, job.hires, cuts_between(job, model), job.margin_mm, model=model).save(out)
    return {"preview": out, "printer": model.key, "tape_mm": tape, "labels": len(images), "bytes": len(data),
            "length_mm": round(label_length_mm(images, job, model.dpi), 1)}


def do_print(job: Job, printer: Printer, dry_run: bool, bin_out: str | None,
             template: str | None = None, vars=None) -> dict:
    if dry_run:
        tape = resolve_tape(job, printer)
        data, images = compile_job(job, tape, printer.model)
        if bin_out:
            Path(bin_out).write_bytes(data)
        return {"dry_run": True, "printer": printer.model.key, "tape_mm": tape, "labels": len(images),
                "bytes": len(data), "length_mm": round(label_length_mm(images, job, printer.model.dpi), 1)}
    from .library import open_default, print_and_record
    return print_and_record(open_default(), printer, job, source="cli", template=template, vars=vars)


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
    ap.add_argument("--printer", "-p", default=None,
                    help="p950nw (network, default) | e560bt (USB) | a network printer host "
                         "(env PTLABEL_DEFAULT_PRINTER)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("printers", help="printers ptlabel can use (USB, PTLABEL_PRINTER, network sweep)")
    p = sub.add_parser("discover", help="find Brother printers on USB and the local network")
    p.add_argument("--timeout", type=float, default=2.0)
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

    p = sub.add_parser("history", help="past prints, newest first")
    p.add_argument("-q", "--query"); p.add_argument("-n", "--limit", type=int, default=20)
    p = sub.add_parser("reprint", help="print a past job again"); p.add_argument("id", type=int)
    p.add_argument("--dry-run", action="store_true")
    sv = sub.add_parser("saved", help="saved labels").add_subparsers(dest="scmd", required=True)
    p = sv.add_parser("list"); p.add_argument("-q", "--query")
    p = sv.add_parser("show"); p.add_argument("id", type=int)
    p = sv.add_parser("save"); p.add_argument("name"); p.add_argument("spec", help="Job/Label JSON file or -")
    p = sv.add_parser("print"); p.add_argument("id", type=int); p.add_argument("--dry-run", action="store_true")
    p = sv.add_parser("delete"); p.add_argument("id", type=int)

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
    try:
        printer = get_printer(a.printer)
    except PrinterError:
        printer = None            # fine for render/schema/decode; status/print report it below
    try:
        result = _run(a, printer)
    except (ValidationError, RenderError, PrinterError, ValueError, KeyError, OSError) as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)}), file=sys.stderr)
        sys.exit(1)
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def _need(printer: Printer | None) -> Printer:
    if printer is None:
        raise PrinterError("no printer: plug one in over USB, set PTLABEL_PRINTER, or pass --printer <host>")
    return printer


def _run(a, printer: Printer | None):
    if a.cmd == "status":
        return _need(printer).status().as_dict()
    if a.cmd == "discover":
        from .discover import discover
        return [f.as_dict() for f in discover(a.timeout)]
    if a.cmd == "printers":
        out = []
        for p in default_printers(scan_network=True).values():
            try:
                st = p.status().as_dict()
            except PrinterError as e:
                st = {"error": str(e)}
            out.append(p.describe() | {"status": st})
        return out
    if a.cmd == "schema":
        s = schemas()
        return s[a.which] if a.which else s
    if a.cmd == "render":
        return do_render(load_job(a.spec, a.tape), a.out, printer)
    if a.cmd == "print":
        return do_print(load_job(a.spec, a.tape), _need(printer), a.dry_run, a.bin)
    if a.cmd == "text":
        job = Job(tape_mm=a.tape, labels=[Label(items=[{"type": "text", "text": a.text.replace("\\n", "\n"),
                                                         "font": a.font}])])
        return do_print(job, _need(printer), False, None) if a.print else do_render(job, a.out, printer)
    if a.cmd == "decode":
        from .decode import decode
        pages = decode(Path(a.bin).read_bytes())
        imgs = [p.image() for p in pages]
        s0 = pages[0].settings
        preview(imgs, s0["tape_mm"], s0.get("hires", False), model=pages[0].model).save(a.out)
        return {"pages": [p.settings | {"end": p.end} for p in pages], "preview": a.out}
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("ptlabel.api:app", host=a.host, port=a.port)
        return None
    if a.cmd in ("history", "reprint", "saved"):
        return _library(a, printer)
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
        rows = _rows(a.rows, _vars(a.var))
        return do_print(job, _need(printer), a.dry_run, None, template=tpl.name,
                        vars=rows if a.rows else rows[0])


def _library(a, printer: Printer | None):
    from .library import Library, job_thumb, print_and_record
    lib = Library()
    if a.cmd == "history":
        return lib.history(a.query, a.limit)
    if a.cmd == "reprint":
        e = lib.print_entry(a.id)
        job = Job.model_validate(e["job"])
        if a.dry_run:
            return do_print(job, _need(printer), True, None)
        return print_and_record(lib, _need(printer), job, source="reprint", reprint_of=a.id,
                                label_id=e["label_id"], template=e["template"], vars=e["vars"])
    if a.scmd == "list":
        return lib.labels(a.query)
    if a.scmd == "show":
        return lib.get(a.id)
    if a.scmd == "save":
        job = load_job(a.spec)
        model = printer.model if printer else P950NW
        return lib.save(a.name, job.model_dump(mode="json", exclude_none=True), "cli",
                        job_thumb(job, job.resolved_tape(12), model))
    if a.scmd == "print":
        job = Job.model_validate(lib.get(a.id)["job"])
        if a.dry_run:
            return do_print(job, _need(printer), True, None)
        return print_and_record(lib, _need(printer), job, source="cli", label_id=a.id)
    if a.scmd == "delete":
        lib.delete(a.id)
        return {"deleted": a.id}


if __name__ == "__main__":
    main()
