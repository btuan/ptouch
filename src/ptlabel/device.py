"""Brother P-touch raster encoding (PT-P950NW over TCP 9100, PT-E560BT over USB).

Source: Brother "Raster Command Reference PT-P900/P900W/P950NW/P910BT" v1.02
(docs/cv_ptp900_eng_raster_102.pdf). Page numbers below refer to that manual.
Brother publishes no manual for the PT-E560BT; its differences (128-pin 180 dpi head,
uncompressed raster, a status reply after ESC i a) come from the ptouch-print driver and
were confirmed on tape (see NOTES.md).

Each raster line runs ACROSS the tape; successive lines go along the feed direction.
Images here are "landscape": height = print-area pins, width = label length.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, replace

from PIL import Image, ImageDraw, ImageFont, ImageOps

PRINTER = os.environ.get("PTLABEL_PRINTER")   # network printer address; no default
PORT = 9100
HEAD_PINS = 560
LINE_BYTES = HEAD_PINS // 8  # 70
DPI = 360

# tape mm -> (left margin pins, print-area pins, right margin pins)   p.20
TZE = {
    6: (240, 64, 256), 9: (219, 106, 235), 12: (197, 150, 213),
    18: (155, 234, 171), 24: (112, 320, 128), 36: (45, 454, 61),
}


@dataclass(frozen=True)
class Model:
    """What differs between P-touch printers. `tape` maps tape mm -> (print pins, first pin)."""
    key: str
    name: str
    dpi: int
    head_pins: int
    tape: dict
    half_cut: bool = True
    hires: bool = True
    compress: bool = True
    min_margin: int = 14             # ESC i d floor, in dots
    usb: tuple[int, int] | None = None
    single_page_last: bool = False   # flag a one-page job as "last page" (ESC i z n9 = 2)

    @property
    def line_bytes(self) -> int:
        return self.head_pins // 8

    @property
    def tape_widths(self) -> list[int]:
        return sorted(self.tape)


# The print area starts after the table's *right*-margin count (213 for 12 mm), not the
# left (197): the first test strip printed ~16 dots too close to the top edge with 197.
P950NW = Model("p950nw", "PT-P950NW", 360, 560, {mm: (a, r) for mm, (_, a, r) in TZE.items()})
# 128-pin head, print area centred (PT-P750W/E550W table, same head family).
E560BT = Model("e560bt", "PT-E560BT", 180, 128,
               {6: (32, 48), 9: (50, 39), 12: (70, 29), 18: (112, 8), 24: (128, 0)},
               hires=False, compress=False, usb=(0x04F9, 0x2203),
               # n9 = 0 on a one-page job: it printed, then sat on "Printing... please wait"
               # and stopped answering USB until power-cycled. 3-page jobs (0/1/2) were fine.
               single_page_last=True)
MODELS = {m.key: m for m in (P950NW, E560BT)}

FONT = "/System/Library/Fonts/Helvetica.ttc"


def mm_to_dots(mm: float, dpi: int = DPI) -> int:
    return round(mm / 25.4 * dpi)


# --------------------------------------------------------------------- render

def text_label(text: str, tape_mm: int = 12, pad: int = 12, marker: bool = False,
               min_len_mm: float = 0) -> Image.Image:
    """1-bit landscape image with `text` centered across the print area.

    marker=True adds a filled square in the top-left corner, a bar along the top
    edge, and a 1-dot box at the exact print-area boundary, so a print shows
    orientation and whether the pin mapping is right.
    """
    area = TZE[tape_mm][1]
    font = ImageFont.truetype(FONT, area - 2 * pad)
    l, t, r, b = font.getbbox(text)
    x0 = 50 if marker else 0
    w = max((r - l) + 24 + x0, mm_to_dots(min_len_mm))   # small fixed end padding
    img = Image.new("1", (w, area), 1)
    d = ImageDraw.Draw(img)
    if marker:
        d.rectangle([0, 0, w - 1, area - 1], outline=0)   # print-area boundary
        d.rectangle([0, 0, 35, 35], fill=0)               # top-left square
        d.rectangle([0, 0, w - 1, 5], fill=0)             # top-edge bar
    d.text((x0 + (w - x0 - (r - l)) // 2 - l, (area - (b - t)) // 2 - t), text, font=font, fill=0)
    return img


def stretch_for_hires(img: Image.Image) -> Image.Image:
    """360x720 dpi mode: twice as many raster lines along the feed (p.12)."""
    return img.resize((img.width * 2, img.height), Image.Resampling.NEAREST)


# --------------------------------------------------------------------- encode

def raster_lines(img: Image.Image, tape_mm: int, flip: bool = False,
                 model: Model = P950NW) -> list[bytes]:
    """One line (head_pins/8 bytes) per image column. Pin 0 = MSB of first byte (p.19-20)."""
    left = model.tape[tape_mm][1]
    img = ImageOps.invert(img.convert("L")).convert("1")  # 1 = black dot
    if flip:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    px = img.load()
    out = []
    for x in range(img.width):
        bits = bytearray(model.line_bytes)
        for y in range(img.height):
            if px[x, y]:
                pin = left + y
                bits[pin // 8] |= 0x80 >> (pin % 8)
        out.append(bytes(bits))
    return out


def packbits(data: bytes, line_bytes: int = LINE_BYTES) -> bytes:
    """TIFF PackBits as described on p.38-39."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run > 1:
            out += bytes([(257 - run) & 0xFF, data[i]])
            i += run
            continue
        j = i + 1
        while j < n and j - i < 128 and not (j + 1 < n and data[j] == data[j + 1]):
            j += 1
        out += bytes([j - i - 1]) + data[i:j]
        i = j
    if len(out) > line_bytes:                    # p.38: fall back to one literal run
        out = bytes([len(data) - 1]) + data
    return bytes(out)


def unpackbits(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(data):
        h = data[i]; i += 1
        if h < 128:
            out += data[i:i + h + 1]; i += h + 1
        elif h > 128:
            out += bytes([data[i]]) * (257 - h); i += 1
    return bytes(out)


# --------------------------------------------------------------------- job

@dataclass
class Page:
    lines: list[bytes]
    cut_after: str | None = None    # "half" | "full" | "none": override the job's cut after this page


@dataclass
class JobOptions:
    tape_mm: int = 12
    auto_cut: bool = True        # ESC i M bit 6
    mirror: bool = False         # ESC i M bit 7
    cut_every: int = 1           # ESC i A (1-255; 0 = never)
    half_cut: bool = False       # ESC i K bit 2
    chain: bool = False          # ESC i K bit 3 inverted: chain=True -> no feed/cut after last
    draft: bool = False          # ESC i K bit 0
    hires: bool = False          # ESC i K bit 6 (360x720 dpi)
    margin_mm: float = 2.0       # ESC i d
    compress: bool = True        # M 02 (TIFF) vs M 00
    page_flags: str = "spec"     # ESC i z n9: "spec" = 0/1/2 first/middle/last; "all2"


def _page_header(o: JobOptions, n_lines: int, page_idx: int, n_pages: int,
                 model: Model = P950NW, cut: str | None = None) -> bytes:
    if o.page_flags == "all2" or n_pages == 1:
        which = 2 if o.page_flags == "all2" or model.single_page_last else 0
    else:
        which = 0 if page_idx == 0 else (2 if page_idx == n_pages - 1 else 1)
    media = 0x09 if (o.hires or o.draft) else 0x00   # p.33 note: 09h for hi-res/draft
    h = bytearray()
    h += b"\x1bia\x01"                       # raster mode; p.5 lists it per page
    h += b"\x1biz" + bytes([0x02 | 0x04 | 0x08, media, o.tape_mm, 0]) \
        + n_lines.to_bytes(4, "little") + bytes([which, 0])                 # p.33
    h += b"\x1biM" + bytes([(0x40 if o.auto_cut else 0) | (0x80 if o.mirror else 0)])  # p.35
    half, every = o.half_cut, o.cut_every
    if cut is not None:                      # per-page override (see Page.cut_after)
        half, every = cut == "half", (1 if cut == "full" else 255)
    h += b"\x1biA" + bytes([every])                                         # p.37
    k = (0x01 if o.draft else 0) | (0x04 if half else 0) \
        | (0 if o.chain else 0x08) | (0x40 if o.hires else 0)
    h += b"\x1biK" + bytes([k])                                              # p.36
    floor = model.min_margin * (2 if o.hires else 1)
    margin = max(floor, mm_to_dots(o.margin_mm, model.dpi * (2 if o.hires else 1)))
    h += b"\x1bid" + margin.to_bytes(2, "little")                            # p.37
    h += b"M" + bytes([2 if o.compress else 0])                              # p.38
    return bytes(h)


def build_job(pages: list[Page], o: JobOptions, model: Model = P950NW) -> bytes:
    if o.hires and not model.hires:
        raise ValueError(f"{model.name} has no high-resolution mode")
    if (o.half_cut or any(p.cut_after == "half" for p in pages)) and not model.half_cut:
        raise ValueError(f"{model.name} can't half-cut")
    if o.compress and not model.compress:
        o = replace(o, compress=False)
    job = bytearray(b"\x00" * 200)          # invalidate (p.23)
    job += b"\x1b@"                          # initialize
    for i, p in enumerate(pages):
        job += _page_header(o, len(p.lines), i, len(pages), model, p.cut_after)
        for line in p.lines:
            if o.compress and not any(line):
                job += b"Z"                      # p.6: only valid with TIFF compression
            elif o.compress:
                c = packbits(line, model.line_bytes)
                job += b"G" + len(c).to_bytes(2, "little") + c
            else:
                job += b"G" + len(line).to_bytes(2, "little") + line
        job += b"\x1a" if i == len(pages) - 1 else b"\x0c"              # p.40-41
    return bytes(job)


# --------------------------------------------------------------------- transport

def _snmp(host: str, *oids: str) -> list[str]:
    r = subprocess.run(["snmpget", "-v2c", "-c", "public", "-Oqv", "-t", "2", "-r", "0", host, *oids],
                       capture_output=True, text=True, timeout=15)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "snmpget failed")
    return [v.strip().strip('"') for v in r.stdout.splitlines()]


def _host(host: str | None) -> str:
    host = host or PRINTER
    if not host:
        raise ValueError("no network printer address: set PTLABEL_PRINTER or pass --printer <host>")
    return host


def status(host: str | None = None) -> dict:
    """Tape name, hrPrinterStatus, hrPrinterDetectedErrorState (standard MIBs)."""
    tape, st, err = _snmp(_host(host), "1.3.6.1.2.1.43.8.2.1.12.1.1",
                          "1.3.6.1.2.1.25.3.5.1.1.1",
                          "1.3.6.1.2.1.25.3.5.1.2.1")
    if st.isdigit():   # no MIB files installed (e.g. Docker): snmpget prints the bare number
        st = f"{_HR_STATUS.get(int(st), 'unknown')}({st})"
    return {"tape": tape, "status": st, "errors": err}


_HR_STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warmup"}


def send(job: bytes, host: str | None = None, port: int = PORT) -> None:
    with socket.create_connection((_host(host), port), timeout=10) as s:
        s.sendall(job)


def wait_idle(host: str | None = None, timeout: float = 90, settle: float = 3) -> dict:
    """Poll SNMP until the printer reports idle again (the TCP port gives no replies)."""
    time.sleep(settle)
    end = time.time() + timeout
    while True:
        st = status(host)
        if "idle" in st["status"] or time.time() > end:
            return st
        time.sleep(1)
