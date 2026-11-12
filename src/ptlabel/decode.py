"""Parse a raster job back into pages: settings + image. For tests and debugging."""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from . import device


@dataclass
class DecodedPage:
    settings: dict = field(default_factory=dict)
    lines: list[bytes] = field(default_factory=list)
    end: str = ""        # "FF" (more pages) or "^Z" (last page)

    @property
    def model(self) -> device.Model:
        """Inferred from the raster line length (70 bytes = 560 pins, 16 = 128 pins)."""
        n = len(self.lines[0]) if self.lines else device.LINE_BYTES
        return next(m for m in device.MODELS.values() if m.line_bytes == n)

    def image(self) -> Image.Image:
        """Print-area image in the same orientation render.py produces (black = 0)."""
        tape = self.settings["tape_mm"]
        area, start = self.model.tape[tape]
        img = Image.new("1", (len(self.lines), area), 1)
        px = img.load()
        for x, line in enumerate(self.lines):
            for y in range(area):
                pin = start + y
                if line[pin // 8] & (0x80 >> (pin % 8)):
                    px[x, y] = 0
        return img


def decode(data: bytes) -> list[DecodedPage]:
    i, n = 0, len(data)
    pages: list[DecodedPage] = []
    page = DecodedPage()
    compress = False
    line_bytes = 0
    while i < n:
        b = data[i]
        if b == 0x00:
            i += 1
        elif data.startswith(b"\x1b@", i):
            i += 2
        elif data.startswith(b"\x1bia", i):
            page.settings["mode"] = data[i + 3]; i += 4
        elif data.startswith(b"\x1biz", i):
            p = data[i + 3:i + 13]
            page.settings.update(valid=p[0], media=p[1], tape_mm=p[2],
                                 lines=int.from_bytes(p[4:8], "little"), page_flag=p[8])
            i += 13
        elif data.startswith(b"\x1biM", i):
            v = data[i + 3]
            page.settings.update(auto_cut=bool(v & 0x40), mirror=bool(v & 0x80)); i += 4
        elif data.startswith(b"\x1biA", i):
            page.settings["cut_every"] = data[i + 3]; i += 4
        elif data.startswith(b"\x1biK", i):
            v = data[i + 3]
            page.settings.update(draft=bool(v & 1), half_cut=bool(v & 4),
                                 chain=not (v & 8), hires=bool(v & 0x40)); i += 4
        elif data.startswith(b"\x1bid", i):
            page.settings["margin_dots"] = int.from_bytes(data[i + 3:i + 5], "little"); i += 5
        elif b == ord("M"):
            compress = data[i + 1] == 2
            page.settings["compress"] = compress; i += 2
        elif b == ord("Z"):
            if not compress:
                raise ValueError(f"Z (zero raster) without TIFF compression at byte {i}")
            page.lines.append(bytes(line_bytes or device.LINE_BYTES)); i += 1
        elif b == ord("G"):
            k = int.from_bytes(data[i + 1:i + 3], "little")
            body = data[i + 3:i + 3 + k]
            line = device.unpackbits(body) if compress else body
            line_bytes = line_bytes or len(line)
            if len(line) != line_bytes or line_bytes not in {m.line_bytes for m in device.MODELS.values()}:
                raise ValueError(f"raster line of {len(line)} bytes at byte {i}")
            page.lines.append(line); i += 3 + k
        elif b in (0x0C, 0x1A):
            page.end = "FF" if b == 0x0C else "^Z"
            if page.settings.get("lines") not in (None, len(page.lines)):
                raise ValueError(f"page declares {page.settings['lines']} lines, has {len(page.lines)}")
            pages.append(page)
            page = DecodedPage()
            i += 1
        else:
            raise ValueError(f"unknown byte 0x{b:02x} at offset {i}")
    if page.lines:
        raise ValueError("trailing raster data without a print command")
    return pages
