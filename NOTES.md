# PT-P950NW raster printing — findings

Printer: Brother PT-P950NW at <printer-ip>. Open: 80 (web UI), 515 (LPD), 9100 (raw).
No IPP/AirPrint. Default emulation: P-touch Template.

References (official Brother developer docs; download links in `docs/README.md`):
- `cv_ptp900_eng_raster_102.pdf` — Raster Command Reference v1.02
- `cv_ptp900_eng_ptemp_103.pdf` — P-touch Template Command Reference v1.03

## Confirmed on tape (12 mm TZe, 2026-11-06)

| Topic | Finding |
|---|---|
| Mode switch | `1B 69 61 01` must be sent at the start of **every page** (manual p.5). Sent once per job, only the last page printed; earlier pages fed blank tape. |
| Page flags (`ESC i z` n9) | 0 / 1 / 2 = first / middle / last works. Setting 2 on every page makes each page a job end: full cut after each, padded to the minimum cut length. |
| Pin offset | Print area starts at the table's **right**-margin pin count (213 for 12 mm), not the left (197). With 213 the image is centred, ~0.9 mm from each tape edge. |
| Orientation | MSB-first, pin 0 = first byte; image prints right-reading with no flip. |
| Compression | Uncompressed (`M 00`, 70-byte `G` lines) and TIFF/PackBits (`M 02`) both work. `Z` only with TIFF. |
| Half-cut | Between pages of one job. Half-cut labels can be short (~12 mm observed). |
| Full cut | Every N pages via `ESC i A`; also at the end of every job, even with chain printing on. |
| Minimum cut length | A fully cut piece is padded to ~22 mm. When a label is shorter, the printer half-cuts at the label's real end, then feeds blank and full-cuts (so the label still peels off at its true length). First job after idle has a ~24 mm leader, with a half-cut where the leader meets the first label. |
| Chain printing | Avoids the leader on the next job; the join still gets a full cut, with ~2 mm waste. |
| Hi-res 360x720 | Works (`ESC i K` bit 6, media byte 09h, image twice as long). Text prints at normal size. 2-dot comb at 720 dpi not resolvable on TZe. |
| Mirror | Works (`ESC i M` bit 7). |
| Status | Port 9100 never replies over the network. Use SNMP: tape `1.3.6.1.2.1.43.8.2.1.12.1.1`, `hrPrinterStatus`, `hrPrinterDetectedErrorState`. |

## Tape-efficient batching

Put many labels in **one job** with half-cuts between them and a full cut at the end
(or `cut_every=N`). Each separate job costs a full cut plus padding to ~22 mm.
