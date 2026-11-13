import pytest

from ptlabel import device
from ptlabel.decode import decode
from ptlabel.printer import compile_job
from ptlabel.render import RenderError
from ptlabel.spec import Job
from ptlabel.usbio import UsbStatus

M = device.E560BT


def job(**kw):
    return Job(labels=[{"items": [{"type": "datamatrix", "data": "E560"}, {"type": "text", "text": "Hi"}]},
                       {"items": [{"type": "text", "text": "two"}]}], **kw)


@pytest.mark.parametrize("tape", M.tape_widths)
def test_round_trip_every_tape(tape):
    data, images = compile_job(job(), tape, M)
    pages = decode(data)
    assert len(pages) == 2
    for p, im in zip(pages, images):
        assert p.model is M and all(len(l) == 16 for l in p.lines)
        assert p.image().tobytes() == im.tobytes()
        assert im.height == M.tape[tape][0]


def test_header_matches_e560bt():
    data, _ = compile_job(job(), 12, M)
    s = decode(data)[0].settings
    assert s["compress"] is False and s["half_cut"] is True and s["tape_mm"] == 12
    assert s["cut_every"] == 255                # default separator: half-cut, full cut only at end
    assert s["margin_dots"] == 14               # 2 mm at 180 dpi
    assert b"\x1biz" in data and b"M\x00" in data and b"M\x02" not in data


def test_print_area_is_centred_on_the_head():
    data, _ = compile_job(Job(labels=[{"items": [{"type": "spacer", "width_mm": 3}], "border": True}]), 12, M)
    line = next(l for l in decode(data)[0].lines if any(l))
    pins = [i for i in range(128) if line[i // 8] & (0x80 >> (i % 8))]
    assert pins[0] == 29 and pins[-1] == 29 + 70 - 1


def test_unsupported_features_fail_loudly():
    with pytest.raises(RenderError, match="high-resolution"):
        compile_job(job(hires=True), 12, M)
    with pytest.raises(RenderError, match="36 mm"):
        compile_job(job(), 36, M)


def test_full_cut_separator():
    s = decode(compile_job(job(separator="full_cut"), 12, M)[0])[0].settings
    assert s["half_cut"] is False and s["cut_every"] == 1


def test_status_block_decoding():
    raw = bytes.fromhex("80 20 42 30 7f 30 32 00 00 00 0c 01 00 00 00 00 00 00 00 00"
                        " 00 00 00 00 01 08 00 00 00 00 00 00".replace(" ", ""))
    st = UsbStatus(raw)
    assert st.tape_mm == 12 and st.problems == [] and st.kind == "reply"
    assert st.tape_name() == "12 mm laminated, black on white"
    cover = UsbStatus(raw[:9] + b"\x10" + raw[10:])
    assert cover.problems == ["Cover open"]


def test_single_page_job_is_flagged_last_page():
    one = Job(labels=[{"items": [{"type": "text", "text": "x"}]}])
    assert decode(compile_job(one, 12, M)[0])[0].settings["page_flag"] == 2
    assert decode(compile_job(one, 12, device.P950NW)[0])[0].settings["page_flag"] == 0
    assert [p.settings["page_flag"] for p in decode(compile_job(job(), 12, M)[0])] == [0, 2]


class FakeUsb:
    """Fake endpoints: the printer is silent for `busy` polls, then answers with `reply`."""
    def __init__(self, busy, reply):
        self.busy, self.reply, self.polls, self.data = busy, reply, 0, b""

    def write(self, data, timeout=None):
        if data == b"\x1biS":
            self.polls += 1
        else:
            self.data += data

    def read(self, n, timeout=None):
        import usb.core
        if self.polls and self.polls > self.busy and not getattr(self, "_answered", 0) == self.polls:
            self._answered = self.polls
            return self.reply
        raise usb.core.USBTimeoutError("timeout", None, None)


def _device(monkeypatch, fake):
    from ptlabel import usbio
    dev = usbio.UsbDevice(0x04F9, 0x2203)
    monkeypatch.setattr(dev, "_open", lambda: (None, fake, fake))
    monkeypatch.setattr(usbio, "_release", lambda d: None)
    monkeypatch.setattr(usbio.time, "sleep", lambda s: None)
    return dev


IDLE = bytes.fromhex("802042307f30320000000c010000000000000000000000000108000000000000")


def test_usb_print_waits_for_the_printer_to_answer_again(monkeypatch):
    fake = FakeUsb(busy=4, reply=IDLE)
    r = _device(monkeypatch, fake).print(b"JOB", pages=1, feed_mm=20)
    assert fake.data == b"JOB" and r["confirmed_by"] == "status after busy" and fake.polls == 5


def test_usb_print_stops_on_an_error_reply(monkeypatch):
    from ptlabel.usbio import UsbError
    cover_open = IDLE[:9] + b"\x10" + IDLE[10:]
    with pytest.raises(UsbError, match="Cover open"):
        _device(monkeypatch, FakeUsb(busy=2, reply=cover_open)).print(b"JOB", pages=1, feed_mm=20)
