"""Render `spec.Label` -> 1-bit landscape image sized for the print head.

Output image: height = print-area pins for the tape, width = label length in dots
along the feed (360 dpi, or 720 dpi when hires). Black = 1 bit in mode "1" after
`ImageOps.invert`... callers just pass the result to `device.raster_lines`.
"""
from __future__ import annotations

import base64
import io
import math
from contextvars import ContextVar
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import device, fonts
from .spec import QR, Barcode, DataMatrix, Image as ImageItem, Job, Label, Rule, Spacer, Text

DPI = 360
FONTS = fonts.STYLES      # the only fonts labels use (bundled IBM Plex), same on every machine
_NOTDEF = "\U000F0000"   # private-use codepoint: renders as the font's missing-glyph box
# tape mm -> (tape width in dots, print-area offset from tape edge)   manual p.13
TAPE_DOTS = {6: (84, 10), 9: (128, 11), 12: (170, 10), 18: (256, 11), 24: (340, 10), 36: (512, 29)}

WHITE, BLACK = 255, 0


class RenderError(ValueError):
    """Render failure; `loc` points at the offending spec part, e.g. ["labels", 0, "items", 1]."""
    def __init__(self, msg: str, loc: list | None = None):
        super().__init__(msg)
        self.loc = loc or []


# Print resolution of the printer being rendered for (set by render_label for its duration).
_dpi: ContextVar[int] = ContextVar("dpi", default=DPI)


def dpmm(scale: int = 1) -> float:
    return _dpi.get() / 25.4 * scale


def _font(name: str, px: int, italic: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return fonts.load(name, px, italic)
    except KeyError:
        raise RenderError(f"unknown font {name!r}; use one of: {', '.join(fonts.NAMES)}") from None


def missing_glyphs(text: str, font: str, italic: bool = False) -> list[str]:
    """Characters the font would draw as a missing-glyph box."""
    f = _font(font, 40, italic)
    box = f.getmask(_NOTDEF)
    box_sig = (box.size, bytes(box))
    out = []
    for ch in sorted(set(text) - set(" \n\t")):
        m = f.getmask(ch)
        if (m.size, bytes(m)) == box_sig:
            out.append(ch)
    return out


def _canvas(w: int, h: int) -> Image.Image:
    return Image.new("L", (max(1, math.ceil(w)), max(1, math.ceil(h))), WHITE)


def _place(content: Image.Image, h: int) -> Image.Image:
    """Center `content` vertically in a canvas of height h."""
    out = _canvas(content.width, h)
    out.paste(content, (0, (h - content.height) // 2))
    return out


# ------------------------------------------------------------------ items

def _text_layout(it: Text, px: int):
    """Font, per-line draw positions (baseline-anchored), underline boxes, and the ink bbox.

    Lines are laid out here rather than by Pillow's multiline drawing so underlines can
    follow each line exactly: baselines are `line_spacing` em apart, lines aligned by
    their advance width."""
    f = _font(it.font, px, it.italic)
    ascent, _ = f.getmetrics()
    lines = it.text.split("\n")
    widths = [f.getlength(line) for line in lines]
    full = max(widths)
    step = round(px * it.line_spacing)
    probe = ImageDraw.Draw(_canvas(1, 1))
    ops, unders, box = [], [], None
    for i, (line, w) in enumerate(zip(lines, widths)):
        x = {"left": 0, "center": (full - w) / 2, "right": full - w}[it.align]
        y = ascent + i * step
        ops.append((x, y, line))
        parts = [probe.textbbox((x, y), line, font=f, anchor="ls")] if line.strip() else []
        if it.underline and line.strip():
            thick = max(1, round(px * 0.06))
            top = y + max(1, round(px * 0.1))
            u = (x, top, x + w, top + thick)
            unders.append(u)
            parts.append(u)
        for l, t, r, b in parts:
            box = (l, t, r, b) if box is None else (min(box[0], l), min(box[1], t), max(box[2], r), max(box[3], b))
    if box is None:
        box = (0, 0, 1, 1)
    box = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
    return f, ops, unders, box


def _text(it: Text, h: int, s: int) -> Image.Image:
    if missing := missing_glyphs(it.text, it.font, it.italic):
        raise RenderError(f"font {it.font!r} has no glyph for {''.join(missing)!r}; "
                          "use plain characters, or an image item for symbols")
    avail_h = int(h * it.height_pct / 100)
    max_w = int(it.max_width_mm * dpmm(s)) if it.max_width_mm else None

    def fits(px):
        try:
            *_, (l, t, r, b) = _text_layout(it, px)
        except OSError:          # Pillow 13 FreeType raises on very small sizes
            return False
        return b - t <= avail_h and (max_w is None or r - l <= max_w)

    if it.size_mm:
        px = round(it.size_mm * dpmm(s))
        if not fits(px):
            raise RenderError(f"text {it.text!r} at {it.size_mm} mm doesn't fit the tape")
    else:
        lo, hi = 8, avail_h * 2
        if not fits(lo):
            raise RenderError(f"text {it.text!r} can't fit (too long for max_width_mm?)")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid - 1)
        px = lo
    f, ops, unders, (l, t, r, b) = _text_layout(it, px)
    img = _canvas(r - l, b - t)
    d = ImageDraw.Draw(img)
    for x, y, line in ops:
        d.text((x - l, y - t), line, font=f, fill=BLACK, anchor="ls")
    for x0, y0, x1, y1 in unders:
        d.rectangle([x0 - l, y0 - t, x1 - l - 1, y1 - t - 1], fill=BLACK)
    return _place(img, h)


def _matrix(mat, quiet: int, h: int, pct: float, s: int, what: str, min_mm: float) -> Image.Image:
    n = len(mat) + 2 * quiet
    module = int(h * pct / 100) // n
    if s == 2:
        module -= module % 2          # keep modules exact when squashing 720->360 vertically
    if module < max(1, round(min_mm * dpmm(s))):
        raise RenderError(f"{what} is too dense for this tape ({n} modules); shorten the data, "
                          "use a datamatrix, or use wider tape")
    img = _canvas(n * module, n * module)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(mat):
        for x, v in enumerate(row):
            if v:
                x0, y0 = (x + quiet) * module, (y + quiet) * module
                d.rectangle([x0, y0, x0 + module - 1, y0 + module - 1], fill=BLACK)
    return _place(img, h)


def _qr(it: QR, h: int, s: int) -> Image.Image:
    import segno
    q = segno.make(it.data, error=it.ecc.lower(), micro=False)
    return _matrix(q.matrix, it.quiet_modules, h, it.height_pct, s, "QR code", it.min_module_mm)


def _datamatrix(it: DataMatrix, h: int, s: int) -> Image.Image:
    from ppf.datamatrix import DataMatrix as DM
    if it.gs1:
        from . import gs1
        try:
            dm = gs1.datamatrix(it.data)
        except gs1.GS1Error as e:
            raise RenderError(f"GS1: {e}") from e
    else:
        dm = DM(it.data)
    return _matrix(dm.matrix, it.quiet_modules, h, it.height_pct, s,
                   "GS1 DataMatrix" if it.gs1 else "Data Matrix", it.min_module_mm)


def _barcode(it: Barcode, h: int, s: int) -> Image.Image:
    import barcode
    try:
        bc = barcode.get(it.symbology, it.data)
    except Exception as e:  # python-barcode raises several types for bad data
        raise RenderError(f"invalid {it.symbology} data {it.data!r}: {e}") from e
    pattern = bc.build()[0]
    m = max(1, round(it.module_mm * dpmm())) * s     # along the feed axis
    quiet = 10 * m
    avail = int(h * it.height_pct / 100)
    text_h = int(avail * 0.24) if it.show_text else 0
    bar_h = avail - text_h - (int(avail * 0.04) if it.show_text else 0)
    w = len(pattern) * m + 2 * quiet
    img = _canvas(w, avail)
    d = ImageDraw.Draw(img)
    for i, bit in enumerate(pattern):
        if bit == "1":
            d.rectangle([quiet + i * m, 0, quiet + (i + 1) * m - 1, bar_h - 1], fill=BLACK)
    if it.show_text:
        label = bc.get_fullcode()
        f = _font("sans", text_h)
        l, t, r, b = d.textbbox((0, 0), label, font=f)
        while r - l > w and f.size > 4:
            f = _font("sans", f.size - 1)
            l, t, r, b = d.textbbox((0, 0), label, font=f)
        d.text(((w - (r - l)) // 2 - l, avail - (b - t) - t), label, font=f, fill=BLACK)
    return _place(img, h)


def _load_image(src: str) -> Image.Image:
    if src.startswith("data:"):
        return Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1])))
    p = Path(src).expanduser()
    if not p.is_file():
        raise RenderError(f"image not found: {src}")
    return Image.open(p)


def _image(it: ImageItem, h: int, s: int) -> Image.Image:
    src = _load_image(it.src)
    if src.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", src.size, "white")
        src = Image.alpha_composite(bg, src.convert("RGBA"))
    src = src.convert("L")
    if it.invert:
        src = ImageOps.invert(src)
    th = int(h * it.height_pct / 100)
    tw = max(1, round(src.width * th / src.height))
    src = src.resize((tw, th), Image.Resampling.LANCZOS)
    bw = src.convert("1") if it.dither else src.point(lambda v: 255 if v >= it.threshold else 0)
    return _place(bw.convert("L"), h)


def _item(it, h: int, s: int) -> Image.Image:
    if isinstance(it, Text):
        return _text(it, h, s)
    if isinstance(it, QR):
        return _qr(it, h, s)
    if isinstance(it, DataMatrix):
        return _datamatrix(it, h, s)
    if isinstance(it, Barcode):
        return _barcode(it, h, s)
    if isinstance(it, ImageItem):
        return _image(it, h, s)
    if isinstance(it, Spacer):
        return _canvas(round(it.width_mm * dpmm(s)), h)
    if isinstance(it, Rule):
        img = _canvas(max(1, round(it.thickness_mm * dpmm(s))), h)
        img.paste(BLACK, (0, 0, img.width, h))
        return img
    raise RenderError(f"unknown item {it!r}")


# ------------------------------------------------------------------ label

def render_label(label: Label, tape_mm: int, hires: bool = False,
                 model: device.Model = device.P950NW) -> Image.Image:
    """Return a mode "1" image: height = print-area dots, width = feed dots
    (twice the dpi along the feed when hires)."""
    if tape_mm not in model.tape:
        raise RenderError(f"{model.name} can't use {tape_mm} mm tape (supports {model.tape_widths} mm)")
    if hires and not model.hires:
        raise RenderError(f"{model.name} has no high-resolution mode; turn hires off")
    token = _dpi.set(model.dpi)
    try:
        return _render_label(label, tape_mm, hires, model)
    finally:
        _dpi.reset(token)


def _render_label(label: Label, tape_mm: int, hires: bool, model: device.Model) -> Image.Image:
    s = 2 if hires else 1
    area = model.tape[tape_mm][0]
    H = area * s
    pad = round(label.pad_mm * dpmm(s))
    inner = H - 2 * pad
    if inner < 8:
        raise RenderError("pad_mm leaves no room on this tape")
    parts = []
    for n, it in enumerate(label.items):
        try:
            parts.append(_item(it, inner, s))
        except RenderError as e:
            raise RenderError(str(e), ["items", n]) from e
    gap = round(label.gap_mm * dpmm(s))
    content_w = sum(p.width for p in parts) + gap * (len(parts) - 1)
    if label.length_mm:
        W = round(label.length_mm * dpmm(s))
        if content_w + 2 * pad > W:
            raise RenderError(f"content needs {(content_w + 2 * pad) / dpmm(s):.1f} mm "
                              f"but the label length is set to {label.length_mm} mm")
    else:
        W = content_w + 2 * pad
    free = W - 2 * pad - content_w
    x = pad + {"start": 0, "center": free // 2, "end": free, "between": 0}[label.justify]
    extra = free // (len(parts) - 1) if label.justify == "between" and len(parts) > 1 else 0
    img = _canvas(W, H)
    for p in parts:
        img.paste(p, (x, pad))
        x += p.width + gap + extra
    if label.border:
        t = max(1, s)
        d = ImageDraw.Draw(img)
        for i in range(t):
            d.rectangle([i, i, W - 1 - i, H - 1 - i], outline=BLACK)
    if label.invert:
        img = ImageOps.invert(img)
    if s == 2:
        img = img.resize((W, area), Image.Resampling.BOX)
    return img.point(lambda v: 255 if v >= 128 else 0).convert("1")


def label_spans_mm(images: list[Image.Image], hires: bool, margin_mm: float,
                   dpi: int = DPI) -> list[tuple[float, float]]:
    """(start, end) of each label along the preview strip, in mm, incl. its feed margins."""
    spans, x = [], 0.0
    per_mm = dpi / 25.4
    for im in images:
        w = im.width / (dpi * (2 if hires else 1)) * 25.4 + 2 * round(margin_mm * per_mm) / per_mm
        spans.append((round(x, 2), round(x + w, 2)))
        x += w
    return spans


def render_job(job: Job, tape_mm: int, model: device.Model = device.P950NW) -> list[Image.Image]:
    one_set = []
    for i, l in enumerate(job.labels):
        try:
            one_set.append(render_label(l, tape_mm, job.hires, model))
        except RenderError as e:
            raise RenderError(str(e), ["labels", i, *e.loc]) from e
    return one_set * job.copies          # whole sets, like Job.expanded_labels()


# ------------------------------------------------------------------ preview

TAPE, TAPE_ALT = (250, 250, 247), (238, 240, 233)
HALF_CUT, FULL_CUT = (90, 90, 96), (214, 48, 40)


def _tape_geometry(tape_mm: int, model: device.Model) -> tuple[int, int, int]:
    """(tape height in dots, print-area offset, preview scale factor vs 360 dpi)."""
    if model.dpi == DPI:
        tape_h, off = TAPE_DOTS[tape_mm]
        return tape_h, off, 1
    tape_h = round(tape_mm * model.dpi / 25.4)
    off = max(0, (tape_h - model.tape[tape_mm][0]) // 2)
    return max(tape_h, model.tape[tape_mm][0]), off, DPI // model.dpi


def preview_layout(images: list[Image.Image], tape_mm: int, hires: bool = False,
                   separator: str | list[str] = "half_cut", margin_mm: float = 1.0,
                   model: device.Model = device.P950NW):
    """Where each label goes in the preview (unscaled dots). A full cut starts a new row,
    so the preview shows the separate pieces that come out of the printer.
    Returns (width, height, rows of [(label index, x)], per-gap cut list, tape_h, off, m, gap)."""
    tape_h, off, _ = _tape_geometry(tape_mm, model)
    m = round(margin_mm * model.dpi / 25.4)
    n = len(images)
    cuts = [separator] * max(0, n - 1) if isinstance(separator, str) else list(separator)[:max(0, n - 1)]
    widths = [(im.width // 2 if hires else im.width) + 2 * m for im in images]
    rows, x = [[]], 0
    for i, w in enumerate(widths):
        rows[-1].append((i, x))
        x += w
        if i < len(cuts) and cuts[i] == "full_cut":
            rows.append([])
            x = 0
    rows = [r for r in rows if r]
    gap = round(4 * model.dpi / 25.4)       # 4 mm between pieces
    W = max(sum(widths[i] for i, _ in r) for r in rows) if rows else 1
    H = len(rows) * tape_h + (len(rows) - 1) * gap
    return W, H, rows, cuts, widths, tape_h, off, m, gap


def label_boxes(images, tape_mm, hires=False, separator="half_cut", margin_mm=1.0,
                model: device.Model = device.P950NW) -> list[tuple[float, float, float, float]]:
    """Each label's box in the preview as fractions of its width/height (frame=False),
    for highlighting in the UI."""
    W, H, rows, _, widths, tape_h, _, _, gap = preview_layout(images, tape_mm, hires, separator, margin_mm, model)
    boxes = [None] * len(images)
    for r, row in enumerate(rows):
        y = r * (tape_h + gap)
        for i, x in row:
            boxes[i] = (round(x / W, 5), round(y / H, 5), round((x + widths[i]) / W, 5), round((y + tape_h) / H, 5))
    return boxes


def preview(images: list[Image.Image], tape_mm: int, hires: bool = False,
            separator: str | list[str] = "half_cut", margin_mm: float = 1.0, scale: int = 2,
            frame: bool = True, model: device.Model = device.P950NW, end_cut: bool = False) -> Image.Image:
    """Human-viewable preview: labels on tape with cut marks; a full cut starts a new row.

    `separator` is one cut kind for every gap, or a list (one per gap, see
    printer.cuts_between). `end_cut` marks the full cut after the last label (the job's end
    cut, unless chain printing). Drawn at the printer's dpi; 180 dpi printers are shown at double
    scale so a preview is the same physical size for every model. Without `frame`, the
    space between rows is transparent."""
    W, H, rows, cuts, widths, tape_h, off, m, gap = preview_layout(images, tape_mm, hires, separator,
                                                                   margin_mm, model)
    scale *= _tape_geometry(tape_mm, model)[2]
    imgs = [i.resize((i.width // 2, i.height)) if hires else i for i in images]
    sheet = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    t = max(2, tape_h // 14)              # cut-mark notch size
    for r, row in enumerate(rows):
        y0 = r * (tape_h + gap)
        row_w = sum(widths[i] for i, _ in row)
        strip = Image.new("RGB", (row_w, tape_h), TAPE)
        d = ImageDraw.Draw(strip)
        for i, x in row:
            if i % 2:                     # alternate a faint tint so label boundaries read at a glance
                d.rectangle([x, 0, x + widths[i] - 1, tape_h - 1], fill=TAPE_ALT)
            ink = ImageOps.invert(imgs[i].convert("L"))       # paste only the black dots, keep the tint
            strip.paste((0, 0, 0), (x + m, off), mask=ink)
        for i, x in row[:-1]:             # cuts inside a row: half-cuts (dashed) only; "none" = nothing
            if cuts[i] == "half_cut":
                cx = x + widths[i]
                for y in range(0, tape_h, 6):
                    d.line([(cx, y), (cx, min(tape_h - 1, y + 3))], fill=HALF_CUT)
                d.line([(cx - t // 2, 0), (cx + t // 2, 0)], fill=HALF_CUT)
                d.line([(cx - t // 2, tape_h - 1), (cx + t // 2, tape_h - 1)], fill=HALF_CUT)
        last = row[-1][0]
        if (last < len(cuts) and cuts[last] == "full_cut") or (end_cut and last == len(images) - 1):
            # this piece is cut off here (or the job's end cut): red notches
            cx = row_w - 1
            d.line([(cx, 0), (cx, tape_h - 1)], fill=FULL_CUT, width=1)
            d.polygon([(cx - t, 0), (cx, 0), (cx, t)], fill=FULL_CUT)
            d.polygon([(cx - t, tape_h - 1), (cx, tape_h - 1), (cx, tape_h - 1 - t)], fill=FULL_CUT)
        sheet.paste(strip, (0, y0))
    if not frame:
        return sheet.resize((W * scale, H * scale), Image.Resampling.NEAREST)
    pad = 12
    out = Image.new("RGB", (W + 2 * pad, H + 2 * pad), (230, 230, 235))
    out.paste(sheet, (pad, pad), sheet)
    return out.resize((out.width * scale, out.height * scale), Image.Resampling.NEAREST)
