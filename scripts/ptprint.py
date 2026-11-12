#!/usr/bin/env python3
"""Print a text label on the PT-P950NW.

    python3 ptprint.py "HELLO" --dry-run     # writes out/label.bin + preview, sends nothing
    python3 ptprint.py "HELLO"               # checks SNMP tape, then prints
"""
import argparse
import os
import sys

from ptlabel import device as pt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--tape", type=int, default=12, choices=sorted(pt.TZE))
    ap.add_argument("--margin-mm", type=float, default=2.0)
    ap.add_argument("--flip", action="store_true", help="flip image across the tape")
    ap.add_argument("--half-cut", action="store_true")
    ap.add_argument("--no-cut", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip SNMP tape check")
    a = ap.parse_args()

    img = pt.text_label(a.text, a.tape)
    opts = pt.JobOptions(tape_mm=a.tape, margin_mm=a.margin_mm,
                         auto_cut=not a.no_cut, half_cut=a.half_cut)
    job = pt.build_job([pt.Page(pt.raster_lines(img, a.tape, a.flip))], opts)
    print(f"{a.tape}mm tape, {img.width} raster lines "
          f"({img.width / pt.DPI * 25.4:.1f} mm printed), job {len(job)} bytes")

    if a.dry_run:
        os.makedirs("out", exist_ok=True)
        img.save("out/label_preview.png")
        open("out/label.bin", "wb").write(job)
        print("dry run: wrote out/label_preview.png and out/label.bin, nothing sent")
        return

    if not a.force:
        st = pt.status()
        print(f"printer: {st}")
        if not st["tape"].startswith(f"{a.tape}mm"):
            sys.exit(f"loaded tape {st['tape']!r} doesn't match --tape {a.tape}; aborting")
    pt.send(job)
    print("sent")


if __name__ == "__main__":
    main()
