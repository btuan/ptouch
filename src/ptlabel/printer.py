"""Job spec -> printer bytes, and the printers themselves.

NetPrinter: PT-P950NW over TCP 9100, status over SNMP.
UsbPrinter: PT-E560BT over USB, status from the printer's own replies.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from PIL import Image

from . import device
from .render import RenderError, render_job
from .spec import Job

DEFAULT_HOST = device.PRINTER     # PTLABEL_PRINTER; None until configured


def separator(job: Job, model: device.Model) -> str:
    """The job's separator, or the printer's best default when unset."""
    return job.separator or ("half_cut" if model.half_cut else "full_cut")


def job_options(job: Job, tape_mm: int, model: device.Model = device.P950NW) -> device.JobOptions:
    sep = separator(job, model)
    half = sep == "half_cut"
    if sep == "full_cut":
        cut_every = 1
    else:
        cut_every = job.full_cut_every or 255
    return device.JobOptions(tape_mm=tape_mm, auto_cut=True, mirror=job.mirror,
                             cut_every=cut_every, half_cut=half, chain=job.chain,
                             hires=job.hires, margin_mm=job.margin_mm, compress=True)


def compile_job(job: Job, tape_mm: int,
                model: device.Model = device.P950NW) -> tuple[bytes, list[Image.Image]]:
    """Render and encode. Returns (printer bytes, rendered label images)."""
    if separator(job, model) == "half_cut" and not model.half_cut:
        raise RenderError(f"{model.name} can't half-cut; use separator full_cut or none", ["separator"])
    images = render_job(job, tape_mm, model)
    pages = [device.Page(device.raster_lines(im, tape_mm, model=model)) for im in images]
    return device.build_job(pages, job_options(job, tape_mm, model), model), images


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
    """Common print flow; subclasses supply status() and _send()."""
    model: device.Model = device.P950NW
    key = "printer"
    where = ""

    def status(self) -> Status:
        raise NotImplementedError

    def _send(self, data: bytes, pages: int, wait: bool, feed_mm: float = 0):
        raise NotImplementedError

    def describe(self) -> dict:
        m = self.model
        return {"key": self.key, "name": m.name, "connection": self.where, "dpi": m.dpi,
                "tape_widths": m.tape_widths, "half_cut": m.half_cut, "hires": m.hires}

    def print(self, job: Job, wait: bool = True) -> dict:
        st = self.status()
        if not st.ok:
            raise PrinterError(f"{self.model.name} not ready: {', '.join(st.problems) or st.state}")
        tape = job.resolved_tape(st.tape_mm)
        if st.tape_mm is not None and tape != st.tape_mm:
            raise PrinterError(f"job is for {tape} mm tape but {st.tape} is loaded")
        data, images = compile_job(job, tape, self.model)
        length = label_length_mm(images, job, self.model.dpi)
        after = self._send(data, len(images), wait, length)
        return {"printer": self.key, "tape_mm": tape, "labels": len(images), "bytes": len(data),
                "length_mm": round(length, 1), "after": after}


class NetPrinter(Printer):
    model = device.P950NW
    key = "p950nw"

    def __init__(self, host: str | None = DEFAULT_HOST):
        self.host = host
        self.where = host or "not configured (set PTLABEL_PRINTER)"

    def status(self) -> Status:
        if not self.host:
            raise PrinterError("no network printer address: set PTLABEL_PRINTER")
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

    def _send(self, data: bytes, pages: int, wait: bool, feed_mm: float = 0):
        device.send(data, self.host)
        if not wait:
            return None
        raw = device.wait_idle(self.host)
        if any(int(b, 16) for b in raw["errors"].split()):
            raise PrinterError(f"printer reported an error after printing: {raw}")
        return raw


class UsbPrinter(Printer):
    model = device.E560BT
    key = "e560bt"
    where = "USB"

    def __init__(self, model: device.Model = device.E560BT, serial: str | None = None):
        from .usbio import UsbDevice
        self.model, self.key = model, model.key
        self.dev = UsbDevice(*model.usb, serial=serial)

    def status(self) -> Status:
        from .usbio import UsbError
        try:
            st = self.dev.status()
        except UsbError as e:
            raise PrinterError(f"{self.model.name}: {e}") from e
        except Exception as e:  # libusb access errors, device vanished mid-read
            raise PrinterError(f"{self.model.name} over USB: {e}") from e
        state = "printing" if st.printing else "idle"
        problems = st.problems
        ok = not problems and state == "idle" and st.tape_mm is not None
        if st.tape_mm is None and "No tape" not in problems:
            problems = [*problems, "No tape"]
        return Status(st.tape_mm, st.tape_name(), state, st.raw[8:10].hex(" "), ok, problems)

    def _send(self, data: bytes, pages: int, wait: bool, feed_mm: float = 0):
        from .usbio import UsbError
        try:
            return self.dev.print(data, pages, feed_mm)
        except UsbError as e:
            raise PrinterError(f"{self.model.name}: {e}") from e


def default_printers() -> dict[str, Printer]:
    """The network printer, plus USB printers when libusb is available (PTLABEL_USB=0 disables)."""
    out: dict[str, Printer] = {"p950nw": NetPrinter()}
    if os.environ.get("PTLABEL_USB", "1") != "0":
        from . import usbio
        if usbio.available():
            out["e560bt"] = UsbPrinter()
    return out


def get_printer(name: str | None = None, printers: dict[str, Printer] | None = None) -> Printer:
    """By key ("p950nw", "e560bt"), else treat `name` as a network printer host.
    Default: PTLABEL_DEFAULT_PRINTER, else p950nw."""
    name = name or os.environ.get("PTLABEL_DEFAULT_PRINTER") or "p950nw"
    if printers and name in printers:
        return printers[name]
    if name == "p950nw":
        return NetPrinter()
    if name in device.MODELS and device.MODELS[name].usb:
        return UsbPrinter(device.MODELS[name])
    return NetPrinter(name)


def label_length_mm(images, job: Job, dpi: int = 360) -> float:
    dpi_feed = dpi * (2 if job.hires else 1)
    return sum(im.width / dpi_feed * 25.4 + 2 * job.margin_mm for im in images)
