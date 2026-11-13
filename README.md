# ptlabel

Design and print labels on Brother P-touch printers (PT-P950NW over the network, PT-E560BT
over USB), from code, a CLI, or an HTTP API. Labels are plain JSON, so coding agents (and later an LLM-backed web UI)
can compose them without touching printer internals.

```
JSON spec ──► render (Pillow) ──► raster (Brother raster protocol) ──► TCP 9100 + SNMP status (PT-P950NW)
 Label/Job      text, QR, DataMatrix,     PackBits, half-cuts,           USB + ESC i S status (PT-E560BT)
 Template       barcodes, images          chain, hi-res, mirror
```

| Module | Role |
|---|---|
| `spec.py` | Pydantic models: `Label`, `Job`, item types. **The contract.** |
| `render.py` | Label → 1-bit image sized to the print head; `preview()` for humans |
| `device.py` | Raster command encoding, verified on real tape (see `NOTES.md`) |
| `printer.py` | Job → sub-jobs; network and USB `Printer`s (status, print, tape check) |
| `usbio.py` | USB transport: status replies, print-and-wait |
| `fonts.py`, `gs1.py` | Bundled fonts; GS1 element strings |
| `templates.py` | Label specs with `{{variables}}`, stored in `templates/*.json` |
| `library.py` | Saved labels and print history in SQLite |
| `discover.py` | Find printers on USB and the local network |
| `decode.py` | Parse printer bytes back into settings + images (tests, debugging) |
| `cli.py` / `api.py` | Front ends |

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). Fonts are bundled, so nothing else is
needed to design labels and render previews; printers need the extras below.

```bash
uv sync
uv run pytest
uv run ptlabel serve          # http://127.0.0.1:8765/  (--host 0.0.0.0 to share on the LAN)
```

### macOS

```bash
brew install uv libusb        # libusb: only for USB printers (PT-E560BT)
```

`snmpget` (network printer status) ships with macOS. USB printers need no permissions setup.
ptlabel finds Homebrew's libusb in `/opt/homebrew/lib` or `/usr/local/lib`.

### Linux (Debian / Ubuntu / Raspberry Pi OS / Fedora)

```bash
sudo apt install libusb-1.0-0 snmp            # Fedora: sudo dnf install libusb1 net-snmp-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
```

USB printers: by default only root can open them. Install the udev rule and join the `lp` group:

```bash
sudo cp docs/60-ptouch.rules /etc/udev/rules.d/ && sudo udevadm control --reload
sudo usermod -aG lp "$USER"                    # then log out and back in (or use `sg lp -c ...`)
```

ptlabel detaches the kernel's `usblp` driver from the printer when it prints; CUPS shouldn't be
configured for the same printer. If libusb is installed somewhere unusual, point
`PTLABEL_LIBUSB` at `libusb-1.0.so.0`.

### NixOS

Prebuilt Python wheels can't find system libraries on NixOS, so the scripts fetch what's needed
from nixpkgs into `./.nix` (nothing is added to your profile or system config):

```bash
scripts/setup-nixos.sh                                 # uv sync + libusb, zlib, libstdc++
sg lp -c scripts/run-nixos.sh                          # or keep it running in tmux:
tmux new -d -s ptlabel 'sg lp -c ~/ptlabel/scripts/run-nixos.sh'
```

`run-nixos.sh` reads settings and secrets from `ptlabel.env` (git-ignored; keep it `chmod 600`).
For USB access add the rule from `docs/60-ptouch.rules` to `services.udev.extraRules` and `"lp"`
to your user's `extraGroups` (a one-off `sudo usermod -aG lp $USER` also works until the next
rebuild). The NixOS firewall only allows SSH by default: open the port in
`networking.firewall.allowedTCPPorts`, or reach the UI over SSH:
`ssh -N -L 8765:127.0.0.1:8765 user@host`. `snmpget` (network printers): `nixpkgs#net-snmp`.

### Docker

`compose.yaml` builds an image for network printers (see Deploy below). USB printers aren't
supported in the container (no libusb, no device passthrough); run ptlabel directly on the host
for those.

### Printers

Printers are found automatically: USB printers when ptlabel starts, network printers by an SNMP
sweep of the local /24 (the PT-P950NW doesn't advertise Bonjour). `ptlabel discover` lists every
Brother device found, supported or not; the UI's printer menu has "Find printers…". Choose a
printer with `--printer e560bt` (CLI), `?printer=e560bt` (API) or the picker in the UI.

### Settings

| Variable | Default | |
|---|---|---|
| `PTLABEL_PRINTER` | none | network printer address (IP or hostname), pinned even if discovery misses it |
| `PTLABEL_DEFAULT_PRINTER` | configured, else first found | e.g. `e560bt` |
| `PTLABEL_USB` | `1` | `0` skips USB printers |
| `PTLABEL_LIBUSB` | search | path to `libusb-1.0.so.0` / `.dylib` if it isn't found |
| `PTLABEL_SCAN` | this machine's /24 | subnets to sweep, comma-separated CIDRs |
| `PTLABEL_DISCOVER` | `1` | `0` turns off the server's startup network sweep |
| `PTLABEL_DB` | `~/.local/share/ptlabel/ptlabel.db` | saved labels and print history; `off` disables |
| `PTLABEL_TEMPLATES` | `templates/` | template folder |
| `PTLABEL_TOKEN` | none | require `Authorization: Bearer <token>` (the UI asks for it) |
| `ANTHROPIC_API_KEY` | none | enables "Describe a label" |
| `PTLABEL_MODEL`, `PTLABEL_EFFORT` | `claude-opus-5-5`, `medium` | designer model and effort |
| `PTLABEL_DESIGN_TURNS`, `PTLABEL_DESIGN_CONCURRENCY` | `10`, `2` | designer limits |

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

Fonts are bundled (IBM Plex, SIL Open Font License, in `src/ptlabel/fonts/`) and are the only
ones labels use, so a saved label renders and prints the same on every machine: `sans`,
`sans-light`, `sans-medium`, `sans-bold`, `sans-condensed`, `sans-condensed-bold`, `mono`,
`mono-bold`, `serif`, `serif-bold`. Any text can also be `"italic": true` and/or
`"underline": true`.

Cuts: labels in a job are separated by the job's `separator` (half-cut by default). A label can
override the cut after itself with `"cut_after": "full_cut" | "half_cut" | "none"`; in previews a
full cut starts a new row, so you see the separate pieces that come out. `copies` prints the whole
set N times (A B C, A B C); `copy_cut` sets the cut between copies (default: the separator), and
`full_cut_every` counts every label printed.

GS1: a `datamatrix` with `"gs1": true` takes Application Identifiers in brackets, e.g.
`(01)09501101530003(17)261231(10)LOT7(21)SN42`. It's validated before printing (known AIs, lengths,
GTIN/SSCC/GLN check digits, YYMMDD dates, GS1 character set) and encoded with FNC1, so scanners
report it as GS1 DataMatrix (`]d2`). Supported AIs are listed in `gs1.py`.

Item types: `text`, `qr`, `datamatrix`, `barcode` (code128, code39, ean13, ean8, upca, itf),
`image` (path or data URI), `spacer`, `rule`. Label options: `length_mm`, `justify`, `cut_after`,
`gap_mm`, `pad_mm`, `border`, `invert`. Job options: `copies`, `copy_cut`, `separator`
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

## Saved labels and print history

Every print is logged to a SQLite file: the exact spec sent, when, which printer, tape width,
label count, tape used, and the error if it failed. Jobs can be saved by name and reopened.
In the UI it's the Library tab (save/update, search, open, delete with undo; past prints with
Open and Reprint). The database is `~/.local/share/ptlabel/ptlabel.db`; set `PTLABEL_DB` to move
it, or `PTLABEL_DB=off` to turn history off. In Docker it's `/data/ptlabel.db`.

```bash
uv run ptlabel history -n 10            # recent prints (-q to search)
uv run ptlabel reprint 42               # print history entry 42 again
uv run ptlabel saved save "Spice jars" spec.json
uv run ptlabel saved list | show 3 | print 3 | delete 3
```

## HTTP API

`uv run ptlabel serve` (127.0.0.1:8765; interactive docs at `/docs`). Set `PTLABEL_TOKEN`
to require `Authorization: Bearer <token>`.

| Method | Path | |
|---|---|---|
| GET | `/schema` | JSON Schemas for Label, Job, Template |
| GET | `/printers` | printers available, with dpi, tape widths, half-cut/hi-res support |
| POST | `/printers/discover` | rescan USB + network; also lists unsupported Brother devices |
| GET | `/printer/status` | tape, state, errors (`?printer=` on this and every render/print route) |
| POST | `/render?format=png\|json` | Job or Label → preview |
| POST | `/print?dry_run=` | Job or Label → printer |
| GET/PUT/DELETE | `/templates/{name}` | manage templates |
| POST | `/templates/{name}/render`, `/print` | body `{"vars": {...}}` or `{"rows": [...]}`, optional `"job": {...}` |
| GET/POST | `/labels` | saved labels (`?q=` search); POST `{"name", "job"}` |
| GET/PUT/DELETE | `/labels/{id}` | one saved label; `/labels/{id}/thumb.png` |
| GET | `/history` | past prints (`?q=`, `?printer=`, `?ok=`); `/history/stats` for totals |
| GET | `/history/{id}` | one print, with its job; `/history/{id}/thumb.png` |
| POST | `/history/{id}/reprint` | print it again (`?printer=` to use another printer) |
| POST | `/design` | `{"prompt", "job"?, "tape_mm"?}` → server-sent events: `status`, `preview`, then `done` or `error` |

## Describe a label (Claude)

With `ANTHROPIC_API_KEY` set, the UI shows a "Describe a label" box and `POST /design` works.
`design.py` runs a small agent loop: Claude drafts a Job, calls `render_preview` to see the exact
raster image, fixes problems (spec errors are fed back to it), and finishes with `submit_design`,
optionally proposing a reusable Template. Nothing is printed. It can also read (never change)
your saved labels, templates and print history (`design_tools.py`): "two more like my spice jar
labels", "asset-tag for A-200 to A-210", "same as last time". Long strips are sent to Claude
wrapped into rows, so they stay legible and under the API's 8000 px image limit. If you pass the current job, Claude
edits it ("make the date bigger"). Settings: `PTLABEL_MODEL` (default `claude-opus-5-5`),
`PTLABEL_EFFORT` (`medium`), `PTLABEL_DESIGN_TURNS` (10), `PTLABEL_DESIGN_CONCURRENCY` (2).

## Deploy (Docker, e.g. a Raspberry Pi)

```bash
rsync -a --exclude .venv --exclude .git --exclude out --exclude .env --exclude data ./ user@host:ptlabel/
ssh user@host 'cd ~/ptlabel && docker compose up -d --build'
```

`ptlabel.env` on the host holds `PTLABEL_PRINTER=<printer IP>` and, for Claude design,
`ANTHROPIC_API_KEY=...` (mode 600, not in git). Templates live in
`./data/templates` (seeded from `templates/` on first start). The UI is at http://<host>:8765/ and has no login:
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
