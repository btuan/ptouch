# ptlabel

Design and print labels on Brother P-touch printers (PT-P950NW over the network, PT-E560BT
over USB), from code, a CLI, or an HTTP API. Labels are plain JSON, so coding agents (and later an LLM-backed web UI)
can compose them without touching printer internals.

```
JSON spec ──► render (Pillow) ──► raster (Brother raster protocol) ──► TCP 9100
 Label/Job      text, QR, DataMatrix,     PackBits, half-cuts,           + SNMP status
 Template       barcodes, images          chain, hi-res, mirror
```

| Module | Role |
|---|---|
| `spec.py` | Pydantic models: `Label`, `Job`, item types. **The contract.** |
| `render.py` | Label → 1-bit image sized to the print head; `preview()` for humans |
| `device.py` | Raster command encoding, verified on real tape (see `NOTES.md`) |
| `printer.py` | Job → bytes; `Printer` (status over SNMP, print, tape check) |
| `templates.py` | Label specs with `{{variables}}`, stored in `templates/*.json` |
| `decode.py` | Parse printer bytes back into settings + images (tests, debugging) |
| `cli.py` / `api.py` | Front ends |

## Setup

```bash
uv sync
uv run pytest
```

Set `PTLABEL_PRINTER` to the network printer's IP address or hostname.
USB printers (PT-E560BT) need libusb (`brew install libusb`); they're picked up automatically.
Choose a printer with `--printer e560bt` (CLI), `?printer=e560bt` (API) or the picker in the UI.
Set `PTLABEL_DEFAULT_PRINTER` to change the default, `PTLABEL_USB=0` to skip USB.
Fonts default to macOS system fonts (SF Pro / SF Mono).

## Label spec

A label is a row of items along the tape. Sizes are in mm; items fill the tape height
and text auto-fits unless `size_mm` is set.

```json
{
  "tape_mm": 12,
  "labels": [
    {"items": [
      {"type": "qr", "data": "https://example.com/a/123"},
      {"type": "text", "text": "ASSET 123\nRack B", "font": "sans-bold"}
    ]},
    {"items": [{"type": "barcode", "data": "SKU-4471"}]}
  ],
  "separator": "half_cut"
}
```

Item types: `text`, `qr`, `datamatrix`, `barcode` (code128, code39, ean13, ean8, upca, itf),
`image` (path or data URI), `spacer`, `rule`. Label options: `length_mm`, `justify`,
`gap_mm`, `pad_mm`, `border`, `invert`. Job options: `copies`, `separator`
(`half_cut` | `full_cut` | `none`), `full_cut_every`, `chain`, `hires`, `mirror`, `margin_mm`.
Full schema: `uv run ptlabel schema` or `GET /schema`.

Bad specs fail loudly rather than printing something wrong: unknown fields, mixed tape
widths, text that doesn't fit `length_mm`, QR codes too dense to scan, characters the
font can't draw.

**Saving tape:** put many labels in one job. Labels in a job are separated by half-cuts
(no waste); each separate job costs a full cut, and pieces shorter than ~22 mm get padded.

## CLI

```bash
uv run ptlabel status
uv run ptlabel text "HELLO\nWORLD" -o hello.png      # preview only
uv run ptlabel text "HELLO" --print
uv run ptlabel render spec.json -o preview.png       # spec can be "-" (stdin)
uv run ptlabel print spec.json [--dry-run]
uv run ptlabel template list
uv run ptlabel template render asset-tag --var id=A-1 --var location="Lab" -o tag.png
uv run ptlabel template print asset-tag --rows rows.csv    # one half-cut label per row
uv run ptlabel decode job.bin -o decoded.png
```

Output is JSON; errors go to stderr as `{"error": ..., "detail": ...}` with exit code 1.

## Web UI

`uv run ptlabel serve`, then open http://127.0.0.1:8765/. Design labels item by item,
fill templates (singly or from a pasted spreadsheet), see the exact raster preview on a
to-scale tape with a mm ruler, and print. The UI only ever edits one Job spec, so what you
see is what prints; `{ } Spec` shows that JSON. `scripts/shot.sh` takes headless screenshots
for design review (narrow widths are unreliable in headless Chrome; use a real browser).

## HTTP API

`uv run ptlabel serve` (127.0.0.1:8765; interactive docs at `/docs`). Set `PTLABEL_TOKEN`
to require `Authorization: Bearer <token>`.

| Method | Path | |
|---|---|---|
| GET | `/schema` | JSON Schemas for Label, Job, Template |
| GET | `/printers` | printers available, with dpi, tape widths, half-cut/hi-res support |
| GET | `/printer/status` | tape, state, errors (`?printer=` on this and every render/print route) |
| POST | `/render?format=png\|json` | Job or Label → preview |
| POST | `/print?dry_run=` | Job or Label → printer |
| GET/PUT/DELETE | `/templates/{name}` | manage templates |
| POST | `/templates/{name}/render`, `/print` | body `{"vars": {...}}` or `{"rows": [...]}`, optional `"job": {...}` |
| POST | `/design` | `{"prompt", "job"?, "tape_mm"?}` → server-sent events: `status`, `preview`, then `done` or `error` |

## Describe a label (Claude)

With `ANTHROPIC_API_KEY` set, the UI shows a "Describe a label" box and `POST /design` works.
`design.py` runs a small agent loop: Claude drafts a Job, calls `render_preview` to see the exact
raster image, fixes problems (spec errors are fed back to it), and finishes with `submit_design`,
optionally proposing a reusable Template. Nothing is printed. If you pass the current job, Claude
edits it ("make the date bigger"). Settings: `PTLABEL_MODEL` (default `claude-opus-5-5`),
`PTLABEL_EFFORT` (`medium`), `PTLABEL_DESIGN_TURNS` (10), `PTLABEL_DESIGN_CONCURRENCY` (2).

## Deploy (Raspberry Pi / Docker)

```bash
rsync -a --exclude .venv --exclude .git --exclude out --exclude .env --exclude data ./ user@host:ptlabel/
ssh user@host 'cd ~/ptlabel && docker compose up -d --build'
```

`ptlabel.env` on the host holds `PTLABEL_PRINTER=<printer IP>` and, for Claude design,
`ANTHROPIC_API_KEY=...` (mode 600, not in git). Templates live in
`./data/templates` (seeded from `templates/` on first start). On Linux the fonts are Inter, Roboto
Condensed and DejaVu Sans Mono under the same names (`sans`, `sans-condensed`, `mono`, ...), so
label widths differ slightly from macOS. The UI is at http://<host>:8765/ and has no login:
anyone who can reach it can print and spend API credit. Set `PTLABEL_TOKEN` to require a token.

## For coding agents

1. Read the schema (`ptlabel schema Job`).
2. Write a spec, render it (`ptlabel render - -o x.png`), look at the PNG, iterate.
3. Print with `--dry-run` first to check length and label count, then print.
4. Reusable designs become templates (`PUT /templates/{name}`), validated on save.

## Roadmap

- Text items with lines of different sizes (stacked layouts), which the designer currently can't express.
- MCP server exposing render/print/templates as tools.

Hardware findings and protocol notes: [NOTES.md](NOTES.md).
Brother reference manuals: links in [docs/README.md](docs/README.md). Hardware test scripts: `scripts/`.
