"""The fonts ptlabel draws with: IBM Plex, bundled in ptlabel/fonts (SIL Open Font License).

Labels must look the same on every machine, so text never uses system fonts. One sans
(light / regular / medium / bold, plus a condensed width), one mono and one serif, each
with an italic. Underline is drawn by the renderer, so it works with every style.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
_SANS = ("IBMPlexSans[wdth,wght].ttf", "IBMPlexSans-Italic[wdth,wght].ttf")   # variable: weight 100-700, width 75-100


def _static(family: str, weight: str) -> tuple[str, str, None]:
    italic = "Italic" if weight == "Regular" else f"{weight}Italic"
    return (f"IBMPlex{family}-{weight}.ttf", f"IBMPlex{family}-{italic}.ttf", None)


# name -> (upright file, italic file, (weight, width) axes for the variable sans, else None)
STYLES: dict[str, tuple[str, str, tuple[int, int] | None]] = {
    "sans": (*_SANS, (400, 100)),
    "sans-light": (*_SANS, (300, 100)),
    "sans-medium": (*_SANS, (500, 100)),
    "sans-bold": (*_SANS, (700, 100)),
    "sans-condensed": (*_SANS, (400, 75)),       # Plex's narrowest width (= IBM Plex Sans Condensed)
    "sans-condensed-bold": (*_SANS, (700, 75)),
    "mono": _static("Mono", "Regular"),
    "mono-bold": _static("Mono", "Bold"),
    "serif": _static("Serif", "Regular"),
    "serif-bold": _static("Serif", "Bold"),
}
# Names used by labels saved before the fonts were bundled.
ALIASES = {"helvetica": "sans", "helvetica-bold": "sans-bold"}
NAMES = list(STYLES)
DESCRIPTION = (" | ".join(NAMES) + ". IBM Plex Sans / Mono / Serif, bundled so labels look the "
               "same on every machine; combine with italic and underline.")


def canonical(name: str) -> str | None:
    """The registry name for `name` (resolving old aliases), or None if it isn't one."""
    name = ALIASES.get(name, name)
    return name if name in STYLES else None


@lru_cache(maxsize=256)
def load(name: str, px: int, italic: bool = False) -> ImageFont.FreeTypeFont:
    """A font at `px` pixels (em). BASIC layout everywhere, so the result doesn't depend on
    whether this machine's Pillow has raqm/HarfBuzz."""
    key = canonical(name)
    if key is None:
        raise KeyError(name)
    upright, slanted, axes = STYLES[key]
    f = ImageFont.truetype(str(FONT_DIR / (slanted if italic else upright)), max(1, px),
                           layout_engine=ImageFont.Layout.BASIC)
    if axes:
        f.set_variation_by_axes(list(axes))       # axis order in the file: weight, width
    return f
