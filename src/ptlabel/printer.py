"""Job spec -> printer bytes, and the printer itself (TCP 9100 + SNMP status)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from PIL import Image

from . import device
from .render import render_job
from .spec import Job

DEFAULT_HOST = os.environ.get("PTLABEL_PRINTER", device.PRINTER)


def job_options(job: Job, tape_mm: int) -> device.JobOptions:
    half = job.separator == "half_cut"
    if job.separator == "full_cut":
        cut_every = 1
    else:
        cut_every = job.full_cut_every or 255
    return device.JobOptions(tape_mm=tape_mm, auto_cut=True, mirror=job.mirror,
                             cut_every=cut_every, half_cut=half, chain=job.chain,
                             hires=job.hires, margin_mm=job.margin_mm, compress=True)


def compile_job(job: Job, tape_mm: int) -> tuple[bytes, list[Image.Image]]:
    """Render and encode. Returns (bytes for port 9100, rendered label images)."""
    images = render_job(job, tape_mm)
    pages = [device.Page(device.raster_lines(im, tape_mm)) for im in images]
    return device.build_job(pages, job_options(job, tape_mm)), images


@dataclass
class Status:
    tape_mm: int | None
    tape: str
    state: str           # idle / printing / ... (hrPrinterStatus)
    errors: str          # hrPrinterDetectedErrorState hex
    ok: bool
    problems: list[str]  # plain-language decoding of `errors`

    def as_dict(self):
        return self.__dict__.copy()


# hrPrinterDetectedErrorState (RFC 2790): bit 7 of byte 0 first
_ERROR_BITS = ["Low tape", "No tape", "Low ink ribbon", "No ink ribbon", "Cover open", "Tape jam",
               "Offline", "Needs service", "Input tray missing", "Output tray missing",
               "Marker supply missing", "Output almost full", "Output full", "Input tray empty",
               "Preventive maintenance overdue"]


def decode_errors(hex_bytes: list[str]) -> list[str]:
    out = []
    for i, b in enumerate(hex_bytes[:2]):
        v = int(b, 16)
        for bit in range(8):
            idx = i * 8 + bit
            if v & (0x80 >> bit) and idx < len(_ERROR_BITS):
                out.append(_ERROR_BITS[idx])
    return out


class PrinterError(RuntimeError):
    pass


class Printer:
    def __init__(self, host: str = DEFAULT_HOST):
        self.host = host

    def status(self) -> Status:
        try:
            raw = device.status(self.host)
        except Exception as e:
            raise PrinterError(f"can't reach printer at {self.host} over SNMP: {e}") from e
        m = re.match(r"(\d+(?:\.\d+)?)mm", raw["tape"])
        tape_mm = int(float(m.group(1))) if m else None
        errs = raw["errors"].split()
        problems = decode_errors(errs)
        ok = not any(int(b, 16) for b in errs) and "idle" in raw["status"]
        return Status(tape_mm, raw["tape"], raw["status"].split("(")[0], raw["errors"].strip(), ok, problems)

    def print(self, job: Job, wait: bool = True) -> dict:
        st = self.status()
        if not st.ok:
            raise PrinterError(f"printer not ready: {st.as_dict()}")
        tape = job.resolved_tape(st.tape_mm)
        if st.tape_mm is not None and tape != st.tape_mm:
            raise PrinterError(f"job is for {tape} mm tape but {st.tape} is loaded")
        data, images = compile_job(job, tape)
        device.send(data, self.host)
        after = None
        if wait:
            raw = device.wait_idle(self.host)
            after = raw
            if any(int(b, 16) for b in raw["errors"].split()):
                raise PrinterError(f"printer reported an error after printing: {raw}")
        return {"tape_mm": tape, "labels": len(images), "bytes": len(data),
                "length_mm": round(label_length_mm(images, job), 1), "after": after}


def label_length_mm(images, job: Job) -> float:
    dpi_feed = 720 if job.hires else 360
    return sum(im.width / dpi_feed * 25.4 + 2 * job.margin_mm for im in images)
