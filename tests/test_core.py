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
    render_label(Label(items=[{"type": "text", "text": "A → B ✓ 5°C ½"}]), 12)   # Plex has these
    with pytest.raises(RenderError, match="no glyph"):
        render_label(Label(items=[{"type": "text", "text": "A 漢"}]), 12)


def test_fonts_are_bundled_and_names_are_checked():
    from ptlabel import fonts
    for name in fonts.NAMES:
        for italic in (False, True):
            assert (fonts.FONT_DIR / fonts.STYLES[name][int(italic)]).exists()
            render_label(Label(items=[{"type": "text", "text": "Ab1", "font": name, "italic": italic}]), 12)
    old = Label(items=[{"type": "text", "text": "x", "font": "helvetica-bold"}])     # saved before bundling
    assert render_label(old, 12).tobytes() == render_label(
        Label(items=[{"type": "text", "text": "x", "font": "sans-bold"}]), 12).tobytes()
    with pytest.raises(ValueError, match="unknown font"):
        Label(items=[{"type": "text", "text": "x", "font": "/System/Library/Fonts/SFNS.ttf"}])


def test_italic_and_underline_change_the_ink():
    base = {"type": "text", "text": "Hello\nWorld", "size_mm": 3, "align": "center"}
    plain = render_label(Label(items=[base]), 12)
    under = render_label(Label(items=[base | {"underline": True}]), 12)
    ital = render_label(Label(items=[base | {"italic": True}]), 12)
    assert plain.tobytes() != ital.tobytes()
    # underline adds a solid run of black under each line
    px = under.convert("L").load()
    rows = [y for y in range(under.height) if sum(px[x, y] == 0 for x in range(under.width)) > under.width * 0.5]
    assert len(rows) >= 2


def test_per_label_cuts_use_only_job_level_commands_and_break_preview_rows():
    """The PT-E560BT ignores per-page cut commands (NOTES.md), so per-label cuts must come out as
    merged pages (no cut), a job-wide full-cut-every-N, or back-to-back jobs."""
    from ptlabel.decode import decode
    from ptlabel.printer import compile_job, cuts_between
    from ptlabel.render import label_boxes, preview
    t = lambda s, **kw: {"items": [{"type": "text", "text": s}], **kw}
    job = Job(labels=[t("A"), t("B", cut_after="full_cut"), t("C", cut_after="none"), t("D")])
    assert cuts_between(job) == ["half_cut", "full_cut", "none"]
    data, images = compile_job(job, 12)
    pages = decode(data)
    # A |half| B |full| C+D merged: full after every 2nd page -> one job, "half + full every 2"
    assert len(pages) == 3 and all(p.settings["half_cut"] and p.settings["cut_every"] == 2 for p in pages)
    assert len(pages[2].lines) > len(pages[0].lines) + 5          # C and D joined on one page
    boxes = label_boxes(images, 12, separator=cuts_between(job), margin_mm=job.margin_mm)
    assert boxes[0][1] == boxes[1][1] == 0 and boxes[2][1] > 0.5 and boxes[2][0] == 0      # C starts row 2
    one_row = preview(images, 12, separator="half_cut", margin_mm=job.margin_mm, frame=False)
    two_rows = preview(images, 12, separator=cuts_between(job), margin_mm=job.margin_mm, frame=False)
    assert two_rows.height > one_row.height * 2 and two_rows.width < one_row.width
    assert two_rows.mode == "RGBA" and two_rows.getpixel((two_rows.width - 1, two_rows.height // 2))[3] == 0


def test_irregular_full_cuts_split_into_back_to_back_jobs():
    from ptlabel.decode import decode
    from ptlabel.printer import compile_job
    t = lambda s, **kw: {"items": [{"type": "text", "text": s}], **kw}
    job = Job(labels=[t("A", cut_after="full_cut"), t("B"), t("C"), t("D")])     # A | B C D
    data, _ = compile_job(job, 12)
    pages = decode(data)
    assert data.count(b"\x1b@") == 2                                       # two jobs
    assert [p.end for p in pages] == ["^Z", "FF", "FF", "^Z"]
    assert not pages[0].settings["half_cut"] and all(p.settings["half_cut"] for p in pages[1:])
    assert all(p.settings["cut_every"] == 255 for p in pages)             # full cuts only at job ends
    assert pages[0].settings["chain"] and not pages[-1].settings["chain"]  # chained join: no leader
    # history #6 shape: multi-label pieces are chained too, and kept as separate sub-jobs so USB
    # can send each one only after the previous has printed
    from ptlabel.printer import compile_parts
    job = Job(labels=[t("A"), t("B", cut_after="full_cut"), t("C"), t("D", cut_after="full_cut"), t("E"), t("F"), t("G")])
    parts, _ = compile_parts(job, 12)
    assert [(p.pages, p.chained) for p in parts] == [(2, True), (2, True), (3, False)]
    assert all(decode(p.data)[-1].end == "^Z" for p in parts)


def test_usb_printer_sends_sub_jobs_one_at_a_time(monkeypatch):
    from ptlabel import device
    from ptlabel.printer import Status, UsbPrinter
    calls = []

    class Dev:
        def print(self, data, pages, feed_mm, timeout=120, confirm=True):
            calls.append((pages, confirm)); return {"confirmed_by": "x"}

    p = UsbPrinter.__new__(UsbPrinter)
    p.model, p.key, p.serial, p.dev = device.E560BT, "e560bt", None, Dev()
    monkeypatch.setattr(p, "_status", lambda: Status(12, "12mm", "idle", "00", True, []))
    t = lambda s, **kw: {"items": [{"type": "text", "text": s}], **kw}
    r = p.print(Job(labels=[t("A"), t("B", cut_after="full_cut"), t("C"), t("D", cut_after="full_cut"), t("E"), t("F"), t("G")]))
    assert calls == [(2, False), (2, False), (3, True)] and r["jobs"] == 3


def test_jobs_without_label_cuts_keep_the_verified_job_wide_encoding():
    from ptlabel.decode import decode
    from ptlabel.printer import compile_job
    job = Job(labels=[{"items": [{"type": "text", "text": s}]} for s in "ABCD"], full_cut_every=2)
    s = [p.settings for p in decode(compile_job(job, 12)[0])]
    assert all(x["half_cut"] and x["cut_every"] == 2 for x in s)
