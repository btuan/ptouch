"""Find P-touch printers on USB and on the local network.

USB: every Brother (vendor 04f9) device, matched to a known model by product ID.
Network: an SNMPv1 sweep of the local /24 (broadcast plus each address), reading
sysDescr. Brother printers answer with their model name ("Brother PT-P950NW ..."),
which is how Brother's own tools find them; the PT-P950NW doesn't advertise Bonjour.
No dependencies: the few bytes of SNMP are encoded here.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from dataclasses import asdict, dataclass

from . import device

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"


@dataclass
class Found:
    connection: str                 # "usb" | "network"
    product: str                    # what the device calls itself
    address: str | None = None      # IP for network printers
    serial: str | None = None       # USB serial number
    model: str | None = None        # ptlabel model key, None if unsupported

    @property
    def supported(self) -> bool:
        return self.model is not None

    def as_dict(self) -> dict:
        return asdict(self) | {"supported": self.supported}


def model_for(text: str) -> str | None:
    """Model key whose name appears in a device description, e.g. 'Brother PT-P950NW'."""
    t = text.upper().replace(" ", "")
    for m in device.MODELS.values():
        if m.name.upper().replace(" ", "") in t:
            return m.key
    return None


# ------------------------------------------------------------------ USB

def usb() -> list[Found]:
    try:
        import usb.core
        import usb.util

        from .usbio import _backend
        be = _backend()
    except ImportError:
        return []
    if be is None:
        return []
    by_pid = {m.usb[1]: m for m in device.MODELS.values() if m.usb}
    out = []
    for d in usb.core.find(find_all=True, idVendor=0x04F9, backend=be):
        m = by_pid.get(d.idProduct)
        try:
            product = usb.util.get_string(d, d.iProduct) or ""
            serial = usb.util.get_string(d, d.iSerialNumber)
        except Exception:   # no permission to read strings (Linux without a udev rule)
            product, serial = "", None
        product = product or (m.name if m else f"Brother USB device {d.idProduct:04x}")
        out.append(Found("usb", product, serial=serial, model=m.key if m else model_for(product)))
    return out


# ------------------------------------------------------------------ SNMP (just enough BER)

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _int(v: int) -> bytes:
    return _tlv(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def _oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(enc))
    return _tlv(0x06, body)


def snmp_get_request(oids: list[str], request_id: int = 1, community: str = "public") -> bytes:
    varbinds = b"".join(_tlv(0x30, _oid(o) + b"\x05\x00") for o in oids)
    pdu = _tlv(0xA0, _int(request_id) + _int(0) + _int(0) + _tlv(0x30, varbinds))
    return _tlv(0x30, _int(0) + _tlv(0x04, community.encode()) + pdu)


def _read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    tag, n = buf[i], buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    return tag, buf[i:i + n], i + n


def _decode_oid(b: bytes) -> str:
    parts, v = [b[0] // 40, b[0] % 40], 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def parse_snmp_response(buf: bytes) -> dict[str, bytes]:
    """OID -> raw value bytes from a GetResponse; {} if it isn't one."""
    try:
        _, msg, _ = _read(buf, 0)
        i = 0
        _, _, i = _read(msg, i)                  # version
        _, _, i = _read(msg, i)                  # community
        tag, pdu, _ = _read(msg, i)
        if tag != 0xA2:
            return {}
        j = 0
        for _ in range(3):                       # request-id, error-status, error-index
            _, _, j = _read(pdu, j)
        _, vbl, _ = _read(pdu, j)
        out, k = {}, 0
        while k < len(vbl):
            _, vb, k = _read(vbl, k)
            _, oid, m = _read(vb, 0)
            tag, val, _ = _read(vb, m)
            if tag == 0x04:
                out[_decode_oid(oid)] = val
        return out
    except (IndexError, ValueError):
        return {}


# ------------------------------------------------------------------ network

def local_networks() -> list[ipaddress.IPv4Network]:
    """PTLABEL_SCAN (comma-separated CIDRs), else the /24 of this machine's main address."""
    env = os.environ.get("PTLABEL_SCAN")
    if env:
        return [ipaddress.ip_network(n.strip(), strict=False) for n in env.split(",") if n.strip()]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # picks the default route; sends nothing
        ip = s.getsockname()[0]
    except OSError:
        return []
    finally:
        s.close()
    return [] if ip.startswith("127.") else [ipaddress.ip_network(f"{ip}/24", strict=False)]


def network(timeout: float = 2.0, networks: list[ipaddress.IPv4Network] | None = None) -> list[Found]:
    nets = local_networks() if networks is None else networks
    if not nets:
        return []
    req = snmp_get_request([SYS_DESCR, SYS_NAME], request_id=0x5054)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setblocking(False)
    seen: dict[str, Found] = {}
    try:
        for net in nets:
            targets = [net.broadcast_address, *net.hosts()] if net.num_addresses <= 1024 else [net.broadcast_address]
            for ip in targets:
                try:
                    s.sendto(req, (str(ip), 161))
                except OSError:
                    pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                time.sleep(0.02)
                continue
            except OSError:
                break
            vals = parse_snmp_response(data)
            descr = vals.get(SYS_DESCR, b"").decode("utf-8", "replace").strip()
            if ip in seen or "brother" not in descr.lower():
                continue
            product = re.split(r"\s*[,;]", descr, maxsplit=1)[0]     # drop firmware/MID details
            seen[ip] = Found("network", product, address=ip, model=model_for(descr))
    finally:
        s.close()
    return sorted(seen.values(), key=lambda f: tuple(int(x) for x in f.address.split(".")))


def discover(timeout: float = 2.0) -> list[Found]:
    return usb() + network(timeout)
