"""USB transport for P-touch printers that talk back (e.g. PT-E560BT).

Unlike the PT-P950NW on TCP 9100, a USB printer answers ESC i S with a 32-byte status
block and reports "printing completed" / errors as it goes (raster manual p.24-28).
Needs pyusb and libusb (`brew install libusb` on macOS).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import threading
import time
from dataclasses import dataclass

_thread_lock = threading.Lock()


@contextlib.contextmanager
def _lock():
    """One conversation with the printer at a time, across threads *and* processes: a status
    request from the web server while the CLI is printing hung the PT-E560BT."""
    path = os.path.join(tempfile.gettempdir(), "ptlabel-usb.lock")
    with _thread_lock, open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

ERR1 = {0x01: "No tape", 0x02: "End of tape", 0x04: "Cutter jam", 0x08: "Weak batteries",
        0x10: "Printer in use", 0x40: "High-voltage adapter"}
ERR2 = {0x01: "Wrong tape", 0x02: "Expansion buffer full", 0x04: "Communication error",
        0x08: "Communication buffer full", 0x10: "Cover open", 0x20: "Overheating",
        0x40: "Black marking not detected", 0x80: "System error"}
MEDIA = {0x00: "none", 0x01: "laminated", 0x03: "non-laminated", 0x04: "fabric",
         0x11: "heat-shrink 2:1", 0x13: "flexible ID", 0x17: "heat-shrink 3:1", 0xFF: "incompatible"}
COLORS = {0x01: "white", 0x02: "other", 0x03: "clear", 0x04: "red", 0x05: "blue", 0x06: "yellow",
          0x07: "green", 0x08: "black", 0x09: "clear (white text)", 0x20: "matte white",
          0x21: "matte clear", 0x22: "matte silver", 0x23: "satin gold", 0x24: "satin silver",
          0x40: "fluorescent orange", 0x41: "fluorescent yellow", 0xF0: "cleaning", 0xF1: "stencil",
          0xFF: "incompatible"}
STATUS_TYPES = {0x00: "reply", 0x01: "printing completed", 0x02: "error", 0x04: "turned off",
                0x05: "notification", 0x06: "phase change"}


@dataclass
class UsbStatus:
    raw: bytes

    @property
    def tape_mm(self) -> int | None:
        return self.raw[10] or None

    @property
    def problems(self) -> list[str]:
        e1, e2 = self.raw[8], self.raw[9]
        return [v for k, v in ERR1.items() if e1 & k] + [v for k, v in ERR2.items() if e2 & k]

    @property
    def kind(self) -> str:
        return STATUS_TYPES.get(self.raw[18], hex(self.raw[18]))

    @property
    def printing(self) -> bool:
        return self.raw[19] == 0x01       # phase type: printing state

    def tape_name(self) -> str:
        if not self.tape_mm:
            return "no tape"
        media = MEDIA.get(self.raw[11], "tape")
        text, tape = COLORS.get(self.raw[25], "?"), COLORS.get(self.raw[24], "?")
        return f"{self.tape_mm} mm {media}, {text} on {tape}"


class UsbError(RuntimeError):
    pass


def _backend():
    import usb.backend.libusb1 as b
    be = b.get_backend()
    if be is None:   # Homebrew's libusb isn't on the default search path on Apple Silicon
        for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
            be = b.get_backend(find_library=lambda _x, p=path: p)
            if be:
                break
    return be


def available() -> bool:
    try:
        return _backend() is not None
    except ImportError:
        return False


class UsbDevice:
    """Open per operation: printers sleep and get unplugged, so don't hold the handle."""

    def __init__(self, vid: int, pid: int, serial: str | None = None):
        self.vid, self.pid, self.serial = vid, pid, serial

    def _open(self):
        import usb.core
        import usb.util
        be = _backend()
        if be is None:
            raise UsbError("libusb not found (macOS: brew install libusb)")
        devs = [d for d in usb.core.find(find_all=True, idVendor=self.vid, idProduct=self.pid, backend=be)
                if self.serial is None or usb.util.get_string(d, d.iSerialNumber) == self.serial]
        if not devs:
            raise UsbError("printer not connected over USB (or switched off)")
        d = devs[0]
        try:
            if d.is_kernel_driver_active(0):
                d.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        cfg = d.get_active_configuration()
        intf = cfg[(0, 0)]
        out = usb.util.find_descriptor(intf, custom_match=lambda e: not e.bEndpointAddress & 0x80)
        inp = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress & 0x80)
        return d, out, inp

    @staticmethod
    def _read(inp, timeout: float) -> list[UsbStatus]:
        """Collect whole 32-byte status blocks until `timeout` passes with nothing new."""
        import usb.core
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            try:
                chunk = bytes(inp.read(64, timeout=100))
            except usb.core.USBTimeoutError:
                chunk = b""
            except usb.core.USBError as e:
                if "timed out" in str(e).lower():
                    chunk = b""
                else:
                    raise
            if chunk:
                buf += chunk
            elif len(buf) % 32 == 0 and buf:
                break
            else:
                time.sleep(0.05)
        return [UsbStatus(buf[i:i + 32]) for i in range(0, len(buf) - 31, 32)]

    def status(self) -> UsbStatus:
        with _lock():
            d, out, inp = self._open()
            try:
                self._read(inp, 0.2)                                   # drop stale replies
                out.write(b"\x00" * 100 + b"\x1b@" + b"\x1biS")
                for _ in range(3):
                    got = self._read(inp, 2.0)
                    if got:
                        return got[-1]
                raise UsbError("printer didn't answer the status request")
            finally:
                _release(d)

    def print(self, job: bytes, pages: int, feed_mm: float, timeout: float = 120) -> dict:
        """Send a whole job and wait for it to finish, without talking to the printer mid-job.

        Tape tests on the PT-E560BT: it sends no "printing completed" messages, and an
        ESC i S sent while it was printing hung it until power-cycled. So: listen
        passively for the estimated print time (~20 mm/s plus ~2 s per cut and ~4 s of
        leader/cut at the end), then ask for status with the full preface, which is safe
        once it's idle.
        """
        with _lock():
            d, out, inp = self._open()
            try:
                self._read(inp, 0.2)
                for i in range(0, len(job), 4096):
                    out.write(job[i:i + 4096], timeout=10000)
                kinds: list[str] = []
                completed = 0
                end = time.time() + 4 + feed_mm / 20 + 2 * pages
                while time.time() < end:
                    for st in self._read(inp, 1.0):
                        kinds.append(st.kind)
                        if st.kind == "error" or st.problems:
                            raise UsbError("printer error: " + (", ".join(st.problems) or st.raw.hex()))
                        completed += st.kind == "printing completed"
                    if completed >= pages:
                        return {"confirmed_by": "printing completed", "messages": kinds}
            finally:
                _release(d)
        deadline = time.time() + timeout
        while True:
            st = self.status()
            if st.problems:
                raise UsbError("printer error after printing: " + ", ".join(st.problems))
            if not st.printing:
                return {"confirmed_by": "status idle", "messages": kinds}
            if time.time() > deadline:
                raise UsbError(f"printer still printing after {timeout:.0f} s")
            time.sleep(2)


def _release(d) -> None:
    import usb.util
    try:
        usb.util.dispose_resources(d)
    except Exception:
        pass
