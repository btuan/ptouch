from ptlabel import discover as d
from ptlabel.discover import Found
from ptlabel.printer import NetPrinter, UsbPrinter, build_registry, pick_default


def _response(oid_vals: dict[str, bytes]) -> bytes:
    """A GetResponse built with the module's own encoder pieces."""
    vbs = b"".join(d._tlv(0x30, d._oid(o) + d._tlv(0x04, v)) for o, v in oid_vals.items())
    pdu = d._tlv(0xA2, d._int(7) + d._int(0) + d._int(0) + d._tlv(0x30, vbs))
    return d._tlv(0x30, d._int(0) + d._tlv(0x04, b"public") + pdu)


def test_snmp_request_is_well_formed():
    req = d.snmp_get_request([d.SYS_DESCR], request_id=1)
    assert req[0] == 0x30 and req[1] == len(req) - 2 and b"public" in req
    assert d._oid("1.3.6.1.2.1.1.1.0") == bytes.fromhex("06082b06010201010100")


def test_snmp_response_parse_long_values():
    descr = b"Brother PT-P950NW, Firmware Ver.1.10 " + b"x" * 200      # forces a long-form length
    vals = d.parse_snmp_response(_response({d.SYS_DESCR: descr, d.SYS_NAME: b"BRW123"}))
    assert vals == {d.SYS_DESCR: descr, d.SYS_NAME: b"BRW123"}
    assert d.parse_snmp_response(b"\x30\x02\x01") == {}                 # garbage is ignored


def test_model_matching():
    assert d.model_for("Brother PT-P950NW, Firmware Ver.1.10") == "p950nw"
    assert d.model_for("PT-E560BT") == "e560bt"
    assert d.model_for("Brother HL-L8430CDW") is None


def test_registry_keys_dedupe_and_stay_stable():
    found = [Found("usb", "PT-E560BT", serial="A", model="e560bt"),
             Found("usb", "PT-E560BT", serial="B", model="e560bt"),
             Found("network", "Brother PT-P950NW", address="10.0.0.5", model="p950nw"),
             Found("network", "Brother PT-P950NW", address="10.0.0.9", model="p950nw"),
             Found("network", "Brother HL-L8430CDW", address="10.0.0.7")]
    reg = build_registry(found, host="10.0.0.5")
    assert list(reg) == ["p950nw", "e560bt", "e560bt-2", "p950nw-2"]
    assert reg["p950nw"].source == "configured" and reg["p950nw-2"].host == "10.0.0.9"
    assert isinstance(reg["e560bt-2"], UsbPrinter) and reg["e560bt-2"].serial == "B"
    assert pick_default(reg) is reg["p950nw"]
    again = build_registry(list(reversed(found)), host="10.0.0.5", keep=reg)
    assert again["e560bt"] is reg["e560bt"] and again["e560bt-2"] is reg["e560bt-2"]


def test_registry_without_config_defaults_to_first_found():
    reg = build_registry([Found("usb", "PT-E560BT", serial="A", model="e560bt")], host=None)
    assert pick_default(reg).key == "e560bt" and not isinstance(pick_default(reg), NetPrinter)
    assert pick_default({}) is None
