import pytest
import zxingcpp
from PIL import Image

from ptlabel import device, gs1
from ptlabel.render import render_label
from ptlabel.spec import Label

GOOD = "(01)09501101530003(17)261231(10)ABC123(21)SN42"


def scan(img: Image.Image):
    """Decode the codes on a rendered label (scaled up, with a white border)."""
    big = img.convert("L").resize((img.width * 4, img.height * 4), Image.Resampling.NEAREST)
    canvas = Image.new("L", (big.width + 80, big.height + 80), 255)
    canvas.paste(big, (40, 40))
    return zxingcpp.read_barcodes(canvas)


def test_check_digit_and_element_string():
    assert gs1.check_digit("0950110153000") == 3
    # GS only after variable-length values that aren't last; predefined-length AIs need none
    assert gs1.element_string(GOOD) == "0109501101530003" + "17261231" + "10ABC123" + gs1.GS + "21SN42"
    assert gs1.element_string("(21)SN42(10)LOT") == "21SN42" + gs1.GS + "10LOT"


@pytest.mark.parametrize("bad,msg", [
    ("(01)09501101530004", "check digit"),
    ("(17)261332", "YYMMDD"),
    ("(01)0950110153000", "exactly 14"),
    ("(10)lot with space", "doesn't allow"),
    ("(99", "couldn't read"),
    ("01095011015300", "start with an AI"),
    ("(77)X", "isn't supported"),
    ("(10)A(10)B", "twice"),
])
def test_validation_errors(bad, msg):
    with pytest.raises(gs1.GS1Error, match=msg):
        gs1.parse(bad)


@pytest.mark.parametrize("model", [device.P950NW, device.E560BT])
def test_rendered_gs1_datamatrix_scans_as_gs1(model):
    img = render_label(Label(items=[{"type": "datamatrix", "data": GOOD, "gs1": True, "min_module_mm": 0.1}]),
                       12, model=model)
    codes = scan(img)
    assert codes and codes[0].symbology_identifier == "]d2"          # ]d2 = GS1 DataMatrix
    assert codes[0].content_type == zxingcpp.ContentType.GS1
    assert codes[0].text == GOOD


def test_plain_datamatrix_stays_plain():
    img = render_label(Label(items=[{"type": "datamatrix", "data": "(01)123"}]), 12)
    codes = scan(img)
    assert codes[0].symbology_identifier == "]d1" and codes[0].text == "(01)123"


def test_spec_rejects_bad_gs1_but_allows_template_placeholders():
    with pytest.raises(ValueError, match="check digit"):
        Label(items=[{"type": "datamatrix", "data": "(01)09501101530004", "gs1": True}])
    from ptlabel.templates import Template
    t = Template(name="part", variables={"gtin": {"example": "09501101530003"}, "serial": {"example": "SN1"}},
                 label={"items": [{"type": "datamatrix", "data": "(01){{gtin}}(21){{serial}}", "gs1": True}]})
    t.check()                                                    # placeholders are fine until filled
    assert t.fill({"gtin": "09501101530003", "serial": "SN7"}).items[0].gs1
    with pytest.raises(ValueError, match="check digit"):        # a bad value is caught when filled
        t.fill({"gtin": "09501101530004", "serial": "SN7"})
