"""Declarative label + job specs.

This is the contract every front end shares (CLI, HTTP API, web UI, coding agents,
LLM-generated designs): produce JSON matching `Label` / `Job`, and the rest of the
pipeline (render -> raster -> printer) is deterministic.

All sizes are in millimetres. A label is a horizontal row of items laid out along
the tape; every item gets the full printable height of the tape unless it says
otherwise.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import fonts

TAPE_WIDTHS = (6, 9, 12, 18, 24, 36)


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    height_pct: float = Field(100, gt=0, le=100,
                              description="Share of the printable tape height to use.")


class Text(_Item):
    """Text, one or more lines (use \\n). Auto-sized to fit unless size_mm is set."""
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)
    font: str = Field("sans", description=fonts.DESCRIPTION)
    italic: bool = False
    underline: bool = False
    size_mm: float | None = Field(None, gt=0, description="Font size (em) in mm; omit to auto-fit.")
    align: Literal["left", "center", "right"] = "left"
    max_width_mm: float | None = Field(None, gt=0, description="Shrink text to fit this width.")
    line_spacing: float = Field(1.1, ge=0.8, le=3)

    @field_validator("font")
    @classmethod
    def _known_font(cls, v: str) -> str:
        if fonts.canonical(v) is None:
            raise ValueError(f"unknown font {v!r}; use one of: {', '.join(fonts.NAMES)}")
        return v


class QR(_Item):
    """QR code, sized to the largest whole-dot module that fits the height."""
    type: Literal["qr"] = "qr"
    data: str = Field(min_length=1)
    ecc: Literal["L", "M", "Q", "H"] = "M"
    quiet_modules: int = Field(1, ge=0, le=4)
    min_module_mm: float = Field(0.2, ge=0.07, description="Refuse to render smaller (unscannable) modules.")


class DataMatrix(_Item):
    """ECC200 Data Matrix: denser than QR, good on narrow tape."""
    type: Literal["datamatrix"] = "datamatrix"
    data: str = Field(min_length=1)
    gs1: bool = Field(False, description="GS1 DataMatrix: data is AIs in brackets, e.g. "
                      "(01)09501101530003(17)261231(10)LOT7(21)SN42; validated and encoded with FNC1.")
    quiet_modules: int = Field(1, ge=0, le=4)
    min_module_mm: float = Field(0.2, ge=0.07, description="Refuse to render smaller (unscannable) modules.")

    @model_validator(mode="after")
    def _gs1(self):
        if self.gs1 and "{{" not in self.data:          # templates are checked once filled
            from . import gs1
            gs1.parse(self.data)
        return self


class Barcode(_Item):
    """1-D barcode. Bars run across the tape; text printed underneath if show_text."""
    type: Literal["barcode"] = "barcode"
    data: str = Field(min_length=1)
    symbology: Literal["code128", "code39", "ean13", "ean8", "upca", "itf"] = "code128"
    module_mm: float = Field(0.25, ge=0.07, le=1.0, description="Narrow bar width.")
    show_text: bool = True


class Image(_Item):
    """Raster image (path or data: URI), scaled to height and thresholded/dithered."""
    type: Literal["image"] = "image"
    src: str
    dither: bool = False
    threshold: int = Field(128, ge=1, le=254)
    invert: bool = False


class Spacer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["spacer"] = "spacer"
    width_mm: float = Field(gt=0)


class Rule(BaseModel):
    """Vertical line across the tape, e.g. a separator."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["rule"] = "rule"
    thickness_mm: float = Field(0.3, gt=0, le=5)


Item = Annotated[Union[Text, QR, DataMatrix, Barcode, Image, Spacer, Rule],
                 Field(discriminator="type")]


class Label(BaseModel):
    """One label: items laid out left-to-right along the tape."""
    model_config = ConfigDict(extra="forbid")
    tape_mm: int | None = Field(None, description="Tape width; omit to use the job's / loaded tape.")
    items: list[Item] = Field(min_length=1)
    gap_mm: float = Field(1.5, ge=0, description="Space between items.")
    pad_mm: float = Field(0.4, ge=0, description="Space inside the print area, top/bottom and ends.")
    length_mm: float | None = Field(None, gt=0, description="Fixed label length; omit to fit content.")
    justify: Literal["start", "center", "end", "between"] = "center"
    border: bool = False
    invert: bool = False
    cut_after: Literal["half_cut", "full_cut", "none"] | None = Field(
        None, description="Cut after this label (ignored on the last). Default: the job's separator.")

    @model_validator(mode="after")
    def _tape(self):
        if self.tape_mm is not None and self.tape_mm not in TAPE_WIDTHS:
            raise ValueError(f"tape_mm must be one of {TAPE_WIDTHS}")
        return self


class Job(BaseModel):
    """Labels printed in one job. Labels in a job are separated by half-cuts by default,
    which wastes no tape; the job ends with a full cut (unless chain=True)."""
    model_config = ConfigDict(extra="forbid")
    labels: list[Label] = Field(min_length=1)
    tape_mm: int | None = Field(None, description="Default tape width for labels; omit to auto-detect.")
    copies: int = Field(1, ge=1, le=100)
    separator: Literal["half_cut", "full_cut", "none"] | None = Field(
        None, description="Between labels. Default: half_cut where the printer supports it, "
                          "else full_cut.")
    full_cut_every: int | None = Field(None, ge=1, le=255,
                                       description="Also full-cut every N labels printed (counts across copies).")
    copy_cut: Literal["half_cut", "full_cut", "none"] | None = Field(
        None, description="With copies > 1: the cut between copies of the set. Default: the separator.")
    chain: bool = Field(False, description="Leave the last label in the printer (no feed/cut).")
    hires: bool = Field(False, description="360x720 dpi (sharper text, slower).")
    mirror: bool = False
    margin_mm: float = Field(1.0, ge=1.0, le=127, description="Feed before/after each label.")

    @model_validator(mode="after")
    def _tape(self):
        if self.tape_mm is not None and self.tape_mm not in TAPE_WIDTHS:
            raise ValueError(f"tape_mm must be one of {TAPE_WIDTHS}")
        widths = {l.tape_mm for l in self.labels if l.tape_mm is not None}
        if self.tape_mm is not None:
            widths.add(self.tape_mm)
        if len(widths) > 1:
            raise ValueError(f"all labels in a job must use the same tape, got {sorted(widths)}")
        return self

    def resolved_tape(self, loaded: int | None = None) -> int:
        for t in [self.tape_mm, *(l.tape_mm for l in self.labels), loaded]:
            if t is not None:
                return t
        raise ValueError("tape width unknown: set tape_mm or let the printer report it")

    def expanded_labels(self) -> list[Label]:
        """Printing order: whole sets, copies times (A B C, A B C). Per-label cut_after
        repeats in every set; full_cut_every counts every label printed; copy_cut is the cut
        between sets."""
        return [l for _ in range(self.copies) for l in self.labels]


def schemas() -> dict:
    """JSON Schemas for agents and the web UI."""
    from .templates import Template
    return {"Label": Label.model_json_schema(), "Job": Job.model_json_schema(),
            "Template": Template.model_json_schema()}
