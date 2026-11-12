"""Render `spec.Label` -> 1-bit landscape image sized for the print head.

Output image: height = print-area pins for the tape, width = label length in dots
along the feed (360 dpi, or 720 dpi when hires). Black = 1 bit in mode "1" after
`ImageOps.invert`... callers just pass the result to `device.raster_lines`.
"""
from __future__ import annotations

import base64
import io
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import device
from .spec import QR, Barcode, DataMatrix, Image as ImageItem, Job, Label, Rule, Spacer, Text

DPI = 360
_SF = "/System/Library/Fonts/SFNS.ttf"          # variable font, wide glyph coverage (→ ✓ ° Ω)
_SF_MONO = "/System/Library/Fonts/SFNSMono.ttf"
# name -> (path, ttc index, variation name or None)
if Path(_SF).exists():          # macOS: system fonts
    FONTS = {
        "sans": (_SF, 0, "Regular"),
        "sans-medium": (_SF, 0, "Medium"),
        "sans-bold": (_SF, 0, "Bold"),
        "sans-light": (_SF, 0, "Light"),
        "sans-condensed": (_SF, 0, "Condensed Regular"),
        "sans-condensed-bold": (_SF, 0, "Condensed Bold"),
        "mono": (_SF_MONO, 0, "Regular"),
        "mono-bold": (_SF_MONO, 0, "Bold"),
        "helvetica": ("/System/Library/Fonts/Helvetica.ttc", 0, None),
        "helvetica-bold": ("/System/Library/Fonts/Helvetica.ttc", 1, None),
    }
else:                           # Linux / Docker: Inter, Roboto Condensed, DejaVu Sans Mono (Debian packages)
    _D = os.environ.get("PTLABEL_FONT_DIR", "/usr/share/fonts")
    _inter = lambda w: (f"{_D}/opentype/inter/Inter-{w}.otf", 0, None)
    _cond = lambda w: (f"{_D}/truetype/roboto/unhinted/RobotoCondensed-{w}.ttf", 0, None)
    _mono = lambda w: (f"{_D}/truetype/dejavu/DejaVuSansMono{w}.ttf", 0, None)
    FONTS = {
        "sans": _inter("Regular"), "sans-medium": _inter("Medium"),
        "sans-bold": _inter("Bold"), "sans-light": _inter("Light"),
        "sans-condensed": _cond("Regular"), "sans-condensed-bold": _cond("Bold"),
        "mono": _mono(""), "mono-bold": _mono("-Bold"),
        "helvetica": _inter("Regular"), "helvetica-bold": _inter("Bold"),
    }
_NOTDEF = "\U000F0000"   # private-use codepoint: renders as the font's missing-glyph box
# tape mm -> (tape width in dots, print-area offset from tape edge)   manual p.13
TAPE_DOTS = {6: (84, 10), 9: (128, 11), 12: (170, 10), 18: (256, 11), 24: (340, 10), 36: (512, 29)}

WHITE, BLACK = 255, 0


class RenderError(ValueError):
    """Render failure; `loc` points at the offending spec part, e.g. ["labels", 0, "items", 1]."""
    def __init__(self, msg: str, loc: list | None = None):
        super().__init__(msg)
        self.loc = loc or []


def dpmm(scale: int = 1) -> float:
    return DPI / 25.4 * scale


def _font(name: str, px: int) -> ImageFont.FreeTypeFont:
    path, index, variation = FONTS.get(name, (name, 0, None))
    try:
        f = ImageFont.truetype(path, max(1, px), index=index)
    except OSError as e:
        raise RenderError(f"font not found: {name!r}; built-in names: {sorted(FONTS)}") from e
    if variation:
        try:
            f.set_variation_by_name(variation)
        except (OSError, ValueError):
            pass
    return f


def missing_glyphs(text: str, font: str) -> list[str]:
    """Characters the font would draw as a missing-glyph box."""
    f = _font(font, 40)
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

def _text(it: Text, h: int, s: int) -> Image.Image:
    if missing := missing_glyphs(it.text, it.font):
        raise RenderError(f"font {it.font!r} has no glyph for {''.join(missing)!r}; "
                          "try font 'sans' or 'mono'")
    avail_h = int(h * it.height_pct / 100)
    max_w = int(it.max_width_mm * dpmm(s)) if it.max_width_mm else None
    probe = ImageDraw.Draw(_canvas(1, 1))

    def measure(px):
        f = _font(it.font, px)
        bb = probe.multiline_textbbox((0, 0), it.text, font=f, align=it.align,
                                      spacing=int(px * (it.line_spacing - 1)))
        # Pillow can return fractional bounds (e.g. centred multi-line text on Linux)
        return f, (math.floor(bb[0]), math.floor(bb[1]), math.ceil(bb[2]), math.ceil(bb[3]))

    def fits(px):
        try:
            _, (l, t, r, b) = measure(px)
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
    f, (l, t, r, b) = measure(px)
    img = _canvas(r - l, b - t)
    ImageDraw.Draw(img).multiline_text((-l, -t), it.text, font=f, fill=BLACK, align=it.align,
                                       spacing=int(px * (it.line_spacing - 1)))
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
    return _matrix(DM(it.data).matrix, it.quiet_modules, h, it.height_pct, s, "Data Matrix",
                   it.min_module_mm)


def _barcode(it: Barcode, h: int, s: int) -> Image.Image:
    import barcode
    try:
        bc = barcode.get(it.symbology, it.data)
    except Exception as e:  # python-barcode raises several types for bad data
        raise RenderError(f"invalid {it.symbology} data {it.data!r}: {e}") from e
    pattern = bc.build()[0]
    m = max(1, round(it.module_mm * DPI / 25.4)) * s     # along the feed axis
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

def render_label(label: Label, tape_mm: int, hires: bool = False) -> Image.Image:
    """Return a mode "1" image: height = print-area dots, width = feed dots
    (720 dpi along the feed when hires)."""
    if tape_mm not in device.TZE:
        raise RenderError(f"unsupported tape {tape_mm} mm")
    s = 2 if hires else 1
    area = device.TZE[tape_mm][1]
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
                              f"but length_mm is {label.length_mm}")
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


def label_spans_mm(images: list[Image.Image], hires: bool, margin_mm: float) -> list[tuple[float, float]]:
    """(start, end) of each label along the preview strip, in mm, incl. its feed margins."""
    spans, x = [], 0.0
    for im in images:
        w = im.width / (720 if hires else DPI) * 25.4 + 2 * round(margin_mm * dpmm()) / dpmm()
        spans.append((round(x, 2), round(x + w, 2)))
        x += w
    return spans


def render_job(job: Job, tape_mm: int) -> list[Image.Image]:
    out = []
    for i, l in enumerate(job.labels):
        try:
            img = render_label(l, tape_mm, job.hires)
        except RenderError as e:
            raise RenderError(str(e), ["labels", i, *e.loc]) from e
        out += [img] * job.copies
    return out


# ------------------------------------------------------------------ preview

def preview(images: list[Image.Image], tape_mm: int, hires: bool = False,
            separator: str = "half_cut", margin_mm: float = 1.0, scale: int = 2,
            frame: bool = True) -> Image.Image:
    """Human-viewable RGB preview: labels on a strip of tape with cut marks."""
    tape_h, off = TAPE_DOTS[tape_mm]
    m = round(margin_mm * dpmm())
    imgs = [i.resize((i.width // 2, i.height)) if hires else i for i in images]
    W = sum(i.width + 2 * m for i in imgs)
    strip = Image.new("RGB", (W, tape_h), (250, 250, 247))
    d = ImageDraw.Draw(strip)
    x = 0
    for n, im in enumerate(imgs):
        strip.paste(im.convert("RGB"), (x + m, off))
        x += im.width + 2 * m
        if n < len(imgs) - 1 and separator != "none":
            color = (220, 60, 60) if separator == "full_cut" else (120, 120, 120)
            for y in range(0, tape_h, 6):
                d.line([(x, y), (x, y + 3)], fill=color)
    if not frame:
        return strip.resize((strip.width * scale, strip.height * scale), Image.Resampling.NEAREST)
    pad = 12
    out = Image.new("RGB", (W + 2 * pad, tape_h + 2 * pad), (230, 230, 235))
    out.paste(strip, (pad, pad))
    return out.resize((out.width * scale, out.height * scale), Image.Resampling.NEAREST)
