"""GS1 element strings for GS1 DataMatrix.

Input is the human-readable form, e.g. "(01)09501101530003(17)261231(10)ABC123(21)SN42":
Application Identifiers (AIs) in brackets, each followed by its value. We validate it (known
AIs, lengths, digits, GTIN/SSCC check digits, dates, GS1 character set) and turn it into the
encoded form: FNC1 first, AIs without brackets, and FNC1 (sent as GS, 0x1D) after every
variable-length value that isn't last.

Covers the AIs used for parts, assets, lots and dates (incl. aerospace part marking via GS1);
AIs 90-99 are free-form internal fields.
"""
from __future__ import annotations

import codecs
import re
from datetime import date

GS = "\x1d"          # stands for FNC1 inside the element string
FNC1 = 232           # Data Matrix codeword for FNC1

# AI -> (description, kind, length). kind: "n" digits, "an" GS1 charset; length fixed if
# `fixed`, else the maximum. Extra checks: "check" = GS1 mod-10 check digit, "date" = YYMMDD.
AIS: dict[str, tuple[str, str, int, bool, str | None]] = {
    "00": ("SSCC", "n", 18, True, "check"),
    "01": ("GTIN", "n", 14, True, "check"),
    "02": ("GTIN of contained items", "n", 14, True, "check"),
    "10": ("Batch/lot", "an", 20, False, None),
    "11": ("Production date", "n", 6, True, "date"),
    "13": ("Packaging date", "n", 6, True, "date"),
    "15": ("Best before", "n", 6, True, "date"),
    "16": ("Sell by", "n", 6, True, "date"),
    "17": ("Expiry date", "n", 6, True, "date"),
    "20": ("Variant", "n", 2, True, None),
    "21": ("Serial number", "an", 20, False, None),
    "22": ("Consumer product variant", "an", 20, False, None),
    "240": ("Additional product ID", "an", 30, False, None),
    "241": ("Customer part number", "an", 30, False, None),
    "242": ("Made-to-order variation", "n", 6, False, None),
    "243": ("Packaging component number", "an", 20, False, None),
    "250": ("Secondary serial number", "an", 30, False, None),
    "251": ("Reference to source entity", "an", 30, False, None),
    "253": ("Global document type ID (GDTI)", "an", 30, False, None),
    "254": ("GLN extension", "an", 20, False, None),
    "30": ("Count of items", "n", 8, False, None),
    "37": ("Count of trade items", "n", 8, False, None),
    "400": ("Customer purchase order", "an", 30, False, None),
    "401": ("Consignment number (GINC)", "an", 30, False, None),
    "403": ("Routing code", "an", 30, False, None),
    "410": ("Ship to GLN", "n", 13, True, "check"),
    "411": ("Bill to GLN", "n", 13, True, "check"),
    "412": ("Purchased from GLN", "n", 13, True, "check"),
    "413": ("Ship for GLN", "n", 13, True, "check"),
    "414": ("Location GLN", "n", 13, True, "check"),
    "415": ("Invoicing party GLN", "n", 13, True, "check"),
    "416": ("Production/service location GLN", "n", 13, True, "check"),
    "422": ("Country of origin", "n", 3, True, None),
    "7003": ("Expiry date and time", "n", 10, True, None),
    "7240": ("Protocol ID", "an", 20, False, None),
    "8003": ("Global returnable asset ID (GRAI)", "an", 30, False, None),
    "8004": ("Global individual asset ID (GIAI)", "an", 30, False, None),
    "8006": ("Component of an item (ITIP)", "n", 18, True, None),
    "8020": ("Payment slip reference", "an", 25, False, None),
    **{f"9{i}": ("Company internal information", "an", 90, False, None) for i in range(10)},
}
# GS1 predefined-length AI prefixes: values of these never need an FNC1 separator after them.
PREDEFINED = {"00", "01", "02", "03", "04", "11", "12", "13", "14", "15", "16", "17", "18", "19",
              "20", "31", "32", "33", "34", "35", "36", "41"}
CSET82 = re.compile(r"""^[A-Za-z0-9!"%&'()*+,\-./:;<=>?_]*$""")


class GS1Error(ValueError):
    pass


def check_digit(digits: str) -> int:
    """GS1 mod-10 check digit for the digits before it."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10


def parse(text: str) -> list[tuple[str, str]]:
    """'(01)…(17)…' -> [("01", "…"), ("17", "…")], validated."""
    text = text.strip()
    if not text.startswith("("):
        raise GS1Error("GS1 data must start with an AI in brackets, e.g. (01)09501101530003(10)LOT1")
    fields = re.findall(r"\((\d{2,4})\)([^(]*)", text)
    if "".join(f"({a}){v}" for a, v in fields) != text:
        raise GS1Error("couldn't read the AIs; write them as (AI)value(AI)value with no spaces between")
    if not fields:
        raise GS1Error("no AIs found")
    seen = set()
    for ai, value in fields:
        if ai not in AIS:
            raise GS1Error(f"AI ({ai}) isn't supported; supported: {', '.join(sorted(AIS, key=lambda a: (len(a), a)))}")
        if ai in seen:
            raise GS1Error(f"AI ({ai}) appears twice")
        seen.add(ai)
        name, kind, length, fixed, extra = AIS[ai]
        where = f"({ai}) {name}"
        if not value:
            raise GS1Error(f"{where} is empty")
        if kind == "n" and not value.isdigit():
            raise GS1Error(f"{where} must be digits only, got {value!r}")
        if kind == "an" and not CSET82.match(value):
            bad = "".join(sorted({c for c in value if not CSET82.match(c)}))
            raise GS1Error(f"{where} has characters GS1 doesn't allow: {bad!r}")
        if fixed and len(value) != length:
            raise GS1Error(f"{where} must be exactly {length} digits, got {len(value)}")
        if not fixed and len(value) > length:
            raise GS1Error(f"{where} can be at most {length} characters, got {len(value)}")
        if extra == "check" and int(value[-1]) != check_digit(value[:-1]):
            raise GS1Error(f"{where} check digit is wrong: expected {check_digit(value[:-1])} at the end")
        if extra == "date":
            yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:])
            try:
                date(2000 + yy, mm, dd if dd else 1)     # DD=00 means "end of month" in GS1
            except ValueError:
                raise GS1Error(f"{where} isn't a valid YYMMDD date: {value}") from None
    return fields


def element_string(text: str) -> str:
    """Encoded form without the leading FNC1: AIs+values, GS after variable-length values."""
    fields = parse(text)
    out = []
    for i, (ai, value) in enumerate(fields):
        out.append(ai + value)
        if i < len(fields) - 1 and ai[:2] not in PREDEFINED:
            out.append(GS)
    return "".join(out)


def human(text: str) -> str:
    """The bracketed form, normalised (for printing under the code)."""
    return "".join(f"({a}){v}" for a, v in parse(text))


# ---- Data Matrix codec: ASCII encodation with FNC1 first and for each GS ------------------

def _encode(msg: str, errors: str = "strict"):
    enc, i = [FNC1], 0
    while i < len(msg):
        c = msg[i]
        if c == GS:
            enc.append(FNC1)
        elif c.isdigit() and i + 1 < len(msg) and msg[i + 1].isdigit():
            enc.append(130 + int(msg[i:i + 2]))
            i += 1
        else:
            o = ord(c)
            if o > 127:
                raise ValueError("GS1 data is ASCII only")
            enc.append(o + 1)
        i += 1
    return bytes(enc), len(msg)


def _decode(code: bytes, errors: str = "strict"):
    raise NotImplementedError


codecs.register(lambda name: codecs.CodecInfo(_encode, _decode, name="datamatrix.gs1")
                if name == "datamatrix.gs1" else None)


def datamatrix(text: str):
    """A ppf DataMatrix for GS1 data in bracket form (validated)."""
    from ppf.datamatrix import DataMatrix
    dm = DataMatrix(element_string(text))
    dm.codecs = ["datamatrix.gs1"]      # ASCII with FNC1; the library does ECC and placement
    return dm
