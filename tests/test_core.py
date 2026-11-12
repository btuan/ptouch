"""Offline tests: spec -> render -> raster bytes -> decode, no printer needed."""
import pytest
from PIL import ImageChops
from pydantic import ValidationError

from ptlabel import device
from ptlabel.decode import decode
from ptlabel.printer import compile_job
from ptlabel.render import RenderError, render_label
from ptlabel.spec import Job, Label
from ptlabel.templates import Template


def job(*labels, **kw):
    return Job(tape_mm=kw.pop("tape_mm", 12), labels=[Label(items=l) for l in labels], **kw)


TEXT = [{"type": "text", "text": "HELLO"}]


def test_packbits_matches_manual_example():
    raw = bytes(20) + bytes([0x22, 0x22, 0x23, 0xBA, 0xBF, 0xA2, 0x22, 0x2B])
    assert device.packbits(raw).hex(" ") == "ed 00 ff 22 05 23 ba bf a2 22 2b"
    assert device.unpackbits(device.packbits(raw)) == raw


@pytest.mark.parametrize("tape", [6, 9, 12, 18, 24, 36])
def test_roundtrip_every_tape(tape):
    j = job([{"type": "datamatrix", "data": "x"}, *TEXT], tape_mm=tape)
    data, images = compile_job(j, tape)
    pages = decode(data)
    assert len(pages) == 1
    assert ImageChops.difference(pages[0].image().convert("L"), images[0].convert("L")).getbbox() is None
    assert images[0].height == device.TZE[tape][1]


def test_multipage_structure():
    """Findings from the tape tests: ESC i a 01 on every page; n9 = 0/1/2; FF then ^Z."""
    data, _ = compile_job(job(TEXT, TEXT, TEXT), 12)
    pages = decode(data)
    assert [p.settings["mode"] for p in pages] == [1, 1, 1]
    assert [p.settings["page_flag"] for p in pages] == [0, 1, 2]
    assert [p.end for p in pages] == ["FF", "FF", "^Z"]
    assert all(p.settings["half_cut"] and not p.settings["chain"] for p in pages)


def test_print_area_starts_at_pin_213_on_12mm():
    lab = Label(items=[{"type": "rule", "thickness_mm": 1}], pad_mm=0)
    data, _ = compile_job(Job(tape_mm=12, labels=[lab]), 12)
    line = next(l for l in decode(data)[0].lines if any(l))
    pins = [p for p in range(560) if line[p // 8] & (0x80 >> (p % 8))]
    assert (pins[0], pins[-1]) == (213, 213 + 149)


def test_separator_options():
    full = decode(compile_job(job(TEXT, TEXT, separator="full_cut"), 12)[0])
    assert all(p.settings["cut_every"] == 1 and not p.settings["half_cut"] for p in full)
    every = decode(compile_job(job(TEXT, TEXT, full_cut_every=2), 12)[0])
    assert every[0].settings["cut_every"] == 2 and every[0].settings["half_cut"]
    chained = decode(compile_job(job(TEXT, chain=True), 12)[0])
    assert chained[0].settings["chain"]


def test_hires_doubles_feed_and_sets_media():
    lo = render_label(Label(items=TEXT), 12)
    hi = render_label(Label(items=TEXT), 12, hires=True)
    assert hi.height == lo.height and abs(hi.width - 2 * lo.width) <= 0.02 * hi.width
    p = decode(compile_job(job(TEXT, hires=True), 12)[0])[0]
    assert p.settings["hires"] and p.settings["media"] == 0x09


def test_copies_expand():
    data, images = compile_job(job(TEXT, copies=3), 12)
    assert len(images) == 3 == len(decode(data))


def test_fixed_length_too_short_errors():
    with pytest.raises(RenderError):
        render_label(Label(items=[{"type": "text", "text": "A LONG LABEL"}], length_mm=5), 12)


def test_dense_qr_errors():
    with pytest.raises(RenderError, match="too dense"):
        render_label(Label(items=[{"type": "qr", "data": "x" * 300}]), 12)


def test_bad_barcode_errors():
    with pytest.raises(RenderError):
        render_label(Label(items=[{"type": "barcode", "symbology": "ean13", "data": "abc"}]), 12)


def test_spec_validation():
    with pytest.raises(ValidationError):
        Label(items=[{"type": "text", "text": "x", "colour": "red"}])     # unknown field
    with pytest.raises(ValidationError):
        Job(labels=[Label(items=TEXT, tape_mm=12), Label(items=TEXT, tape_mm=24)])
    with pytest.raises(ValidationError):
        Label(items=TEXT, tape_mm=13)


def test_template_fill_and_check():
    t = Template(name="t", variables={"id": {"example": "A1"}, "loc": {"default": "Lab"}},
                 label={"items": [{"type": "text", "text": "{{id}} {{ loc }}"}]})
    t.check()
    assert t.fill({"id": "Z9"}).items[0].text == "Z9 Lab"
    with pytest.raises(ValueError, match="missing"):
        t.fill({})
    bad = Template(name="b", label={"items": [{"type": "text", "text": "{{nope}}"}]})
    with pytest.raises(ValueError, match="undeclared"):
        bad.check()
    j = t.job([{"id": "1"}, {"id": "2"}], tape_mm=12)
    assert [l.items[0].text for l in j.labels] == ["1 Lab", "2 Lab"]


def test_qr_too_small_on_6mm_suggests_alternatives():
    with pytest.raises(RenderError, match="datamatrix"):
        render_label(Label(items=[{"type": "qr", "data": "x"}]), 6)


def test_missing_glyph_is_an_error_not_a_box():
    render_label(Label(items=[{"type": "text", "text": "A → B ✓"}]), 12)      # SF has these
    with pytest.raises(RenderError, match="no glyph"):
        render_label(Label(items=[{"type": "text", "text": "A → B", "font": "helvetica"}]), 12)
