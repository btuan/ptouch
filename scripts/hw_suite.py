#!/usr/bin/env python3
"""Exercise every PT-P950NW raster feature on ONE strip of tape.

    python3 test_suite.py            # dry run: write out/*.bin + out/suite.png, send nothing
    python3 test_suite.py --send     # print the strip

All jobs except the last use chain printing (no feed/cut at job end) and every
label is separated by a half-cut, so the whole suite comes out as a single
strip with one leader and one final cut. Expected strip (feed order):

    ORIENT ? A1 ~ A2 | A3 ? HI-RES ? MIRROR ? END |

    ~  half-cut (backing intact)        |  full cut
    ?  boundary between chained jobs: whatever appears here is a finding
       (expected: half-cut, since each job has half-cut on)
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw

from ptlabel import device as pt

TAPE = 12
LABEL_MM = 10      # short labels; tells us if the 27 mm minimum applies to half-cut labels
MARGIN_MM = 1      # minimum feed margin (p.17)


def label(text, **kw):
    return pt.text_label(text, TAPE, pad=38, min_len_mm=LABEL_MM, **kw)


def hires_label(text):
    """Text stretched to 360x720, then a comb of 1-dot lines every 2 dots drawn in
    720-dpi space (finer than 360 dpi can print)."""
    txt = pt.stretch_for_hires(label(text))
    comb_w = 120
    img = Image.new("1", (txt.width + comb_w + 30, txt.height), 1)
    img.paste(txt, (0, 0))
    d = ImageDraw.Draw(img)
    for x in range(txt.width + 15, txt.width + 15 + comb_w, 2):
        d.line([(x, 15), (x, img.height - 15)], fill=0)
    return img


def opts(**kw):
    base = dict(tape_mm=TAPE, half_cut=True, cut_every=255, margin_mm=MARGIN_MM, chain=True)
    base.update(kw)
    return pt.JobOptions(**base)


# name, page images, options, what it proves
JOBS = [
    ("orient", [label("ORIENT", marker=True)], opts(compress=False),
     "uncompressed path; square+bar on one edge, thin box just inside both tape edges"),
    ("cuts", [label("A1"), label("A2"), label("A3")], opts(cut_every=2),
     "multi-page job, TIFF compression, full cut after every 2nd label: A1 ~ A2 | A3"),
    ("hires", [hires_label("HI-RES")], opts(hires=True),
     "360x720: text same length as others, comb shows distinct fine lines"),
    ("mirror", [label("MIRROR")], opts(mirror=True),
     "text reads backwards"),
    ("end", [label("END")], opts(chain=False),
     "last job: not chained, so feed + full cut releases the whole strip"),
]


def build(imgs, o):
    pages = [pt.Page(pt.raster_lines(im, o.tape_mm)) for im in imgs]
    for p in pages:
        for line in p.lines:
            assert pt.unpackbits(pt.packbits(line)) == line
    return pt.build_job(pages, o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually print")
    a = ap.parse_args()

    os.makedirs("out", exist_ok=True)
    built = []
    total_mm = 0.0
    for name, imgs, o, why in JOBS:
        job = build(imgs, o)
        open(f"out/{name}.bin", "wb").write(job)
        dpi_feed = 720 if o.hires else pt.DPI
        mm = sum(im.width / dpi_feed * 25.4 + 2 * MARGIN_MM for im in imgs)
        total_mm += mm
        built.append((name, job, why))
        print(f"{name:7s} {len(imgs)} label(s) ~{mm:4.0f} mm  {len(job):6d} bytes  {why}")
    print(f"\nprinted length ~{total_mm:.0f} mm (+ one leader; more if the printer "
          f"pads each half-cut label to 27 mm)")

    rows = [im if not o.hires else im.resize((im.width // 2, im.height))
            for _, imgs, o, _ in JOBS for im in imgs]
    sheet = Image.new("1", (sum(im.width + 12 for im in rows), rows[0].height), 1)
    x = 0
    for im in rows:
        sheet.paste(im, (x, 0)); x += im.width + 12
    sheet.save("out/suite.png")

    if not a.send:
        print("dry run: wrote out/*.bin and out/suite.png (strip as it should look); nothing sent")
        return

    st = pt.status()
    print(f"\nprinter: {st}")
    if not st["tape"].startswith(f"{TAPE}mm"):
        sys.exit(f"loaded tape {st['tape']!r} isn't {TAPE}mm; aborting")
    for name, job, _ in built:
        pt.send(job)
        st = pt.wait_idle()
        print(f"{name:7s} sent; status={st['status']} errors={st['errors']}")
        if any(int(b, 16) for b in st["errors"].split()):
            sys.exit("printer reports an error; stopping (tape may be held inside: "
                     "run `python3 ptprint.py END` to feed and cut)")


if __name__ == "__main__":
    main()
