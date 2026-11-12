#!/usr/bin/env python3
"""Diagnostic strip: why did pages ending in FF (0x0C) print blank?

First strip finding: in a 3-page job only the last page (ends 0x1A) printed;
pages 1-2 fed ~23 mm of blank tape with no half-cut between them.

Now fixed per manual p.5-6: ESC i a 01 is sent at the start of EVERY page, and
Z (zero raster) is only used with TIFF compression. Print area moved to pin 213.

Three 2-page jobs, chained so there is one leader (chain joins get a full cut):

    V1  P1 ~ P2   per-manual structure, TIFF, n9 = 0 / 2       (+ alignment marker on P1)
    V2  Q1 ~ Q2   same, but n9 = 2 on every page
    V3  R1 ~ R2   per-manual structure, uncompressed            (last job: feed + cut)

    python3 diag_multipage.py           # dry run
    python3 diag_multipage.py --send
"""
import argparse
import os
import sys

from ptlabel import device as pt

TAPE = 12


def label(text, **kw):
    return pt.text_label(text, TAPE, pad=38, min_len_mm=10, **kw)


def opts(**kw):
    base = dict(tape_mm=TAPE, half_cut=True, cut_every=255, margin_mm=1, chain=True)
    base.update(kw)
    return pt.JobOptions(**base)


JOBS = [
    ("V1", [label("P1", marker=True), label("P2")], opts()),
    ("V2", [label("Q1"), label("Q2")], opts(page_flags="all2")),
    ("V3", [label("R1"), label("R2")], opts(compress=False, chain=False)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    os.makedirs("out", exist_ok=True)
    built = []
    for name, imgs, o in JOBS:
        job = pt.build_job([pt.Page(pt.raster_lines(im, TAPE)) for im in imgs], o)
        open(f"out/diag_{name}.bin", "wb").write(job)
        mm = sum(im.width / pt.DPI * 25.4 + 2 for im in imgs)
        print(f"{name}: {len(imgs)} pages ~{mm:.0f} mm, {len(job)} bytes")
        built.append((name, job))
    if not a.send:
        print("dry run; nothing sent")
        return
    st = pt.status()
    print(f"printer: {st}")
    if not st["tape"].startswith(f"{TAPE}mm"):
        sys.exit("wrong tape; aborting")
    for name, job in built:
        pt.send(job)
        st = pt.wait_idle()
        print(f"{name} sent; status={st['status']} errors={st['errors']}")
        if any(int(b, 16) for b in st["errors"].split()):
            sys.exit("printer error; stopping")


if __name__ == "__main__":
    main()
