"""Job spec -> printer bytes, and the printers themselves.

NetPrinter: PT-P950NW over TCP 9100, status over SNMP.
UsbPrinter: PT-E560BT over USB, status from the printer's own replies.
"""
from __future__ import annotations

import os
import re
import time
import subprocess
from dataclasses import dataclass, replace

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


def cuts_between(job: Job, model: device.Model = device.P950NW) -> list[str]:
    """What happens after each printed label but the last: "half_cut" | "full_cut" | "none".
    A label's own cut_after wins; otherwise full_cut_every, then the job's separator."""
    labels = job.expanded_labels()
    n = len(job.labels)
    default = separator(job, model)
    out = []
    for i, lab in enumerate(labels[:-1]):
        set_end = i % n == n - 1                 # between two copies of the set
        if set_end and job.copy_cut:
            out.append(job.copy_cut)
        elif lab.cut_after and not set_end:      # a label's own cut; the last label's has no effect
            out.append(lab.cut_after)
        elif job.full_cut_every and (i + 1) % job.full_cut_every == 0:
            out.append("full_cut")
        else:
            out.append(default)
    return out


@dataclass
class SubJob:
    """One complete printer job (init ... ^Z). A print is one or more of these, back to back."""
    data: bytes
    pages: int
    feed_mm: float          # rough tape length, for timing the wait
    chained: bool           # no feed after it: the next sub-job's start cuts at the join


def compile_job(job: Job, tape_mm: int,
                model: device.Model = device.P950NW) -> tuple[bytes, list[Image.Image]]:
    """Render and encode. Returns (printer bytes, rendered label images)."""
    parts, images = compile_parts(job, tape_mm, model)
    return b"".join(p.data for p in parts), images


def compile_parts(job: Job, tape_mm: int,
                  model: device.Model = device.P950NW) -> tuple[list[SubJob], list[Image.Image]]:
    """Render and encode as sub-jobs (usually one). Returns (sub-jobs, rendered label images)."""
    if separator(job, model) == "half_cut" and not model.half_cut:
        raise RenderError(f"{model.name} can't half-cut; use separator full_cut or none", ["separator"])
    if (any(l.cut_after == "half_cut" for l in job.labels) or job.copy_cut == "half_cut") and not model.half_cut:
        raise RenderError(f"{model.name} can't half-cut", ["labels"])
    images = render_job(job, tape_mm, model)
    opts = job_options(job, tape_mm, model)
    raster = lambda im: device.Page(device.raster_lines(im, tape_mm, model=model))
    feed = lambda ims: label_length_mm(ims, job, model.dpi)
    if not (any(l.cut_after for l in job.labels[:-1]) or (job.copy_cut is not None and job.copies > 1)):
        # no per-label cuts: the job-wide encoding verified on tape (NOTES.md)
        data = device.build_job([raster(im) for im in images], opts, model)
        return [SubJob(data, len(images), feed(images), opts.chain)], images
    return _per_label_cut_job(images, cuts_between(job, model), job, opts, model, raster, feed), images


def _per_label_cut_job(images, cuts, job: Job, opts: device.JobOptions, model: device.Model,
                       raster, feed) -> list[SubJob]:
    """Per-label cuts from job-level commands only: the PT-E560BT applies the first page's cut
    settings to the whole job (tape test, NOTES.md), so per-page cut commands can't be used.

    1. Labels joined by "none" become one page (nothing to cut).
    2. Between pages only half and full cuts remain. If the full cuts fall every N pages, one job
       with "half-cut + full cut every N" says it; otherwise the job is split at each full cut
       into back-to-back jobs, each ending with the normal end-of-job full cut."""
    m = device.mm_to_dots(job.margin_mm, model.dpi * (2 if job.hires else 1))
    groups, gaps = [[images[0]]], []
    for im, cut in zip(images[1:], cuts):
        if cut == "none":
            groups[-1].append(im)
        else:
            gaps.append(cut)
            groups.append([im])
    pages = [_join(g, 2 * m) for g in groups]
    half = "half_cut" in gaps
    full = [i for i, c in enumerate(gaps) if c == "full_cut"]
    one = lambda o: [SubJob(device.build_job([raster(p) for p in pages], o, model), len(pages), feed(images), o.chain)]
    if not full:
        return one(replace(opts, half_cut=half, cut_every=255))
    n = full[0] + 1
    if full == [i for i in range(len(gaps)) if (i + 1) % n == 0]:
        return one(replace(opts, half_cut=half, cut_every=n))
    out, start = [], 0
    for i in [*full, len(pages) - 1]:
        seg = pages[start:i + 1]
        last = i == len(pages) - 1
        # Chain every sub-job but the last: the next starts without a leader and the printer
        # full-cuts at the join (verified on tape). Over USB each sub-job is sent only after the
        # previous one has printed (UsbPrinter._send): data arriving mid-join hung the PT-E560BT.
        chain = opts.chain if last else True
        seg_opts = replace(opts, half_cut=half and len(seg) > 1, cut_every=255, chain=chain)
        seg_labels = [im for g in groups[start:i + 1] for im in g]
        out.append(SubJob(device.build_job([raster(p) for p in seg], seg_opts, model), len(seg), feed(seg_labels), chain))
        start = i + 1
    return out


def _join(images: list[Image.Image], gap: int) -> Image.Image:
    """Labels that run into each other (no cut), as one image with the usual margin between."""
    if len(images) == 1:
        return images[0]
    w = sum(im.width for im in images) + gap * (len(images) - 1)
    out = Image.new("1", (w, images[0].height), 1)
    x = 0
    for im in images:
        out.paste(im, (x, 0))
        x += im.width + gap
    return out


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

    STATUS_TTL = 15.0     # seconds: previews and UI polls reuse a recent answer (or failure)

    def status(self, max_age: float | None = None) -> Status:
        """Printer status, reusing an answer (or failure) younger than `max_age` seconds.

        An offline network printer takes seconds to time out, and every preview needs the
        loaded tape. So with the default max_age an expired answer is still served at once
        while a background thread asks again (stale-while-revalidate). max_age=0 (print(),
        the UI's Retry) always waits for a fresh answer."""
        fresh = max_age == 0
        max_age = self.STATUS_TTL if max_age is None else max_age
        cached = getattr(self, "_cached", None)
        if cached and time.monotonic() - cached[0] >= max_age and not fresh:
            self._revalidate()
        elif not cached or fresh:
            cached = self._fetch()
        if isinstance(cached[1], PrinterError):
            raise cached[1]
        return cached[1]

    def _fetch(self):
        try:
            entry = (time.monotonic(), self._status())
        except PrinterError as e:
            entry = (time.monotonic(), e)
        self._cached = entry
        return entry

    def _revalidate(self) -> None:
        import threading
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True

        def run():
            try:
                self._fetch()
            finally:
                self._refreshing = False
        threading.Thread(target=run, daemon=True).start()

    def _status(self) -> Status:
        raise NotImplementedError

    def _send(self, parts: list[SubJob], wait: bool):
        raise NotImplementedError

    source = "configured"            # or "discovered"

    def describe(self) -> dict:
        m = self.model
        return {"key": self.key, "name": m.name, "connection": self.where, "source": self.source,
                "dpi": m.dpi, "tape_widths": m.tape_widths, "half_cut": m.half_cut, "hires": m.hires}

    def identity(self) -> tuple:
        """What makes two Printer objects the same physical printer."""
        return (self.model.key, self.where)

    def print(self, job: Job, wait: bool = True) -> dict:
        st = self.status(max_age=0)
        if not st.ok:
            raise PrinterError(f"{self.model.name} not ready: {', '.join(st.problems) or st.state}")
        tape = job.resolved_tape(st.tape_mm)
        if st.tape_mm is not None and tape != st.tape_mm:
            raise PrinterError(f"job is for {tape} mm tape but {st.tape} is loaded")
        parts, images = compile_parts(job, tape, self.model)
        length = label_length_mm(images, job, self.model.dpi)
        try:
            after = self._send(parts, wait)
        finally:
            self._cached = None
        return {"printer": self.key, "tape_mm": tape, "labels": len(images), "bytes": sum(len(p.data) for p in parts),
                "jobs": len(parts), "length_mm": round(length, 1), "after": after}


class NetPrinter(Printer):
    model = device.P950NW
    key = "p950nw"

    def __init__(self, host: str | None = DEFAULT_HOST, model: device.Model = device.P950NW):
        self.host, self.model, self.key = host, model, model.key
        self.where = host or "not configured (set PTLABEL_PRINTER)"

    def _status(self) -> Status:
        if not self.host:
            raise PrinterError("no network printer address: set PTLABEL_PRINTER")
        try:
            raw = device.status(self.host)
        except subprocess.TimeoutExpired as e:
            raise PrinterError(f"no answer from the printer at {self.host} (is it on, and on this network?)") from e
        except FileNotFoundError as e:
            raise PrinterError("snmpget not found: install net-snmp to talk to network printers") from e
        except Exception as e:
            if "timeout" in str(e).lower() or "no response" in str(e).lower():
                raise PrinterError(f"no answer from the printer at {self.host} "
                                   "(is it on, and on this network?)") from e
            raise PrinterError(f"can't reach printer at {self.host} over SNMP: {e}") from e
        m = re.match(r"(\d+(?:\.\d+)?)mm", raw["tape"])
        tape_mm = int(float(m.group(1))) if m else None
        errs = raw["errors"].split()
        problems = decode_errors(errs)
        ok = not any(int(b, 16) for b in errs) and "idle" in raw["status"]
        return Status(tape_mm, raw["tape"], raw["status"].split("(")[0], raw["errors"].strip(), ok, problems)

    def _send(self, parts: list[SubJob], wait: bool):
        device.send(b"".join(p.data for p in parts), self.host)
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
        self.model, self.key, self.serial = model, model.key, serial
        self.dev = UsbDevice(*model.usb, serial=serial)

    def identity(self) -> tuple:
        return (self.model.key, "usb", self.serial)

    def describe(self) -> dict:
        return super().describe() | {"serial": self.serial}

    def _status(self) -> Status:
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

    def _send(self, parts: list[SubJob], wait: bool):
        """One sub-job at a time: the next is sent only once the previous should have printed.
        Between sub-jobs we only wait (a status request starts with an initialize command, which
        could break a chained join); the full idle check runs after the last."""
        from .usbio import UsbError
        try:
            result = None
            for k, part in enumerate(parts):
                last = k == len(parts) - 1
                result = self.dev.print(part.data, part.pages, part.feed_mm, confirm=last)
            return result | {"jobs": len(parts)}
        except UsbError as e:
            raise PrinterError(f"{self.model.name}: {e}") from e


def build_registry(found: list | None = None, host: str | None = DEFAULT_HOST,
                   keep: dict[str, Printer] | None = None) -> dict[str, Printer]:
    """Printers keyed "p950nw", "e560bt" (then "e560bt-2", ...): the configured network printer
    (PTLABEL_PRINTER) plus supported printers from discovery. Objects in `keep` are reused
    when they're the same printer, so keys stay stable across rescans."""
    out: dict[str, Printer] = {}
    old = {p.identity(): p for p in (keep or {}).values()}

    candidates: list[tuple[Printer, str]] = []
    if host:
        candidates.append((NetPrinter(host), "configured"))
    for f in found or []:
        if not f.supported:
            continue
        m = device.MODELS[f.model]
        if f.connection == "network":
            candidates.append((NetPrinter(f.address, model=m), "discovered"))
        elif m.usb:
            candidates.append((UsbPrinter(m, serial=f.serial), "discovered"))
    # printers seen before go first and keep their keys; new ones fill in after
    keys_before = {ident: p.key for ident, p in old.items()}
    candidates.sort(key=lambda c: (c[1] != "configured", c[0].identity() not in keys_before))
    taken: set[tuple] = set()
    for p, source in candidates:
        ident = p.identity()
        if ident in taken:
            continue
        taken.add(ident)
        p = old.get(ident, p)
        p.source = "configured" if source == "configured" else p.source if ident in old else source
        k = keys_before.get(ident)
        if k is None or k in out:
            base, k, n = p.model.key, p.model.key, 2
            while k in out or (k in keys_before.values() and keys_before.get(ident) != k):
                k, n = f"{base}-{n}", n + 1
        p.key = k
        out[k] = p
    return out


def default_printers(scan_network: bool = False) -> dict[str, Printer]:
    """Configured printer + USB printers found now (+ a network sweep if asked).
    PTLABEL_USB=0 skips USB."""
    from . import discover
    found = discover.usb() if os.environ.get("PTLABEL_USB", "1") != "0" else []
    if scan_network:
        found += discover.network()
    return build_registry(found)


def pick_default(printers: dict[str, Printer]) -> Printer | None:
    """PTLABEL_DEFAULT_PRINTER if present, else the configured printer, else the first found."""
    want = os.environ.get("PTLABEL_DEFAULT_PRINTER")
    if want in printers:
        return printers[want]
    return next((p for p in printers.values() if p.source == "configured"), None) \
        or next(iter(printers.values()), None)


def get_printer(name: str | None = None, printers: dict[str, Printer] | None = None) -> Printer:
    """By key ("p950nw", "e560bt", "e560bt-2"), else treat `name` as a network printer host.
    Without a name: the default from pick_default(), discovering USB printers if needed."""
    if printers is None:
        printers = default_printers()
    if name is None:
        p = pick_default(printers)
        if p is None:
            raise PrinterError("no printer: plug one in over USB, set PTLABEL_PRINTER, "
                               "or run `ptlabel discover`")
        return p
    if name in printers:
        return printers[name]
    if name in device.MODELS and device.MODELS[name].usb:
        return UsbPrinter(device.MODELS[name])
    if name in device.MODELS:
        return NetPrinter(model=device.MODELS[name])
    return NetPrinter(name)


def label_length_mm(images, job: Job, dpi: int = 360) -> float:
    dpi_feed = dpi * (2 if job.hires else 1)
    return sum(im.width / dpi_feed * 25.4 + 2 * job.margin_mm for im in images)
