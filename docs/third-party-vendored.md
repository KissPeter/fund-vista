# Third-party vendored elements (fund-vista backend)

Issue #58 (3/7). Inventory of everything vendored into the backend repo with
its origin and licence. Licence texts for the fonts live next to the files
(see per-directory `README.md`); this file is the single index. The machine
counterpart is `backend/THIRD_PARTY_NOTICES.md` (Python dependencies +
copyright lines, suitable for ToS/UI attribution).

Decisions D1–D4 (issue #58, not renegotiated here):

- D1: Potrace (GPL-2.0-or-later) and vpype-flow-imager (GPL-3.0) are
  server-side-only components and may be imported; their output (SVG/G-code)
  is not GPL. Neither is currently vendored or even installed
  (`backend/penplot/methods.py` documents the deliberately permissive
  OpenCV/NumPy tracing stack instead).
- D2: none of these ever reach the browser or ship to clients.
- D3: generated images (SVG/plot) carry **no footer/credit/attribution**;
  attribution lives in the ToS, the UI and API metadata.
- D4: new dependencies only per the shop repo `docs/third-party-policy.md`
  allowlist.

## 1. Fonts

| Path | Face | Copyright | Licence | Licence file | Upstream |
|---|---|---|---|---|---|
| `backend/penplot/fonts/Znikoslsvginot8-GOB3y.ttf` | ZnikoSL-SVGinOT-8 (`znikoslsvginot`) | (c) 2011-2021 gluk (www.glukfonts.pl), Reserved Font Names Znikomit and ZnikoSL | SIL OFL 1.1 | `backend/penplot/fonts/OFL-1.1.txt` | https://www.glukfonts.pl (Grzegorz Luka) |
| `backend/penplot/fonts/Excalifont-Regular.ttf` | Excalifont (`excalifont`) | (c) 2024 by Excalidraw (font: Jan Filipek / DizajnDesign) | SIL OFL 1.1 (upstream `OFL.txt`; embedded name table holds only the (c) notice) | `backend/penplot/fonts/OFL-1.1.txt` | https://github.com/excalidraw/excalifont |
| `backend/penplot/fonts/ComicShanns-Regular.ttf` | Comic Shanns (`comic-shanns`) | Copyright 2020 Shannon Miwa | **MIT — NOT OFL** (grant embedded in nameID 13) | `backend/penplot/fonts/MIT.txt` | https://github.com/shannpersand/comic-shanns |
| `backend/penplot/fonts/Nunito-Regular.ttf` | Nunito (`nunito`) | Copyright 2014 The Nunito Project Authors (upstream author Vernon Adams) | SIL OFL 1.1 | `backend/penplot/fonts/OFL-1.1.txt` | https://github.com/googlefonts/nunito (Excalidraw weight-500 Latin-subset build) |
| `backend/wordmark/fonts/Poppins-Bold.ttf` | Poppins Bold (`poppins-bold`) | Copyright 2020 The Poppins Project Authors; Indian Type Foundry; Ninad Kale (Devanagari), Jonny Pinhorn (Latin) | SIL OFL 1.1 | `backend/wordmark/fonts/OFL-1.1.txt` | https://github.com/itfoundry/Poppins |

Per-directory details: `backend/penplot/fonts/README.md`,
`backend/wordmark/fonts/README.md`. All five copyright lines above were read
from the fonts' embedded name tables (fontTools `name` records 0/8/13/14),
not copied from comments.

## 2. Preview background textures

Display-only JPEGs embedded as fill-only `<image>` in the served SVG; they
never enter geometry, stats or the plotter recipe
(`backend/penplot/backgrounds.py`, `BACKGROUNDS`).

| Path | Id | Provenance (recorded in `backgrounds.py`) | Licence |
|---|---|---|---|
| `backend/penplot/backgrounds/dark-texture.jpg` | `dark-texture` | https://www.ilmarket.hu/cdn/shop/files/078231209_0_1448x2006.jpg?v=1789241014 (white e-commerce frame trimmed at vendor time) | **UNKNOWN — not clarified.** Product photo from a webshop CDN; no licence statement found. Kept display-only; replacement is a separate decision (not made here). |
| `backend/penplot/backgrounds/light-texture.jpg` | `light-texture` | https://s13emagst.akamaized.net/products/132357/132356154/images/res_c62ebfeeaa56d62efb5003123f1de3e1.jpg?width=720&height=720&hash=7032669015377C063DF4421280F4AD24 (white e-commerce frame trimmed at vendor time) | **UNKNOWN — not clarified.** Same situation as above; replacement is a separate decision (not made here). |

## 3. Hershey stroke tables (code, not files)

Single-stroke plotter glyph data compiled into Python modules (no vendored
binary). Sources already noted in the module docstrings, collected here:

- `backend/penplot/hershey_font.py` (simplex subset, A-Z 0-9 + marks):
  Hershey Fonts originally created by Dr. A. V. Hershey at the U.S. National
  Bureau of Standards (required acknowledgement, Usenet Font Consortium
  distribution); JSON organisation from Stewart C. Russell's
  [python-hershey](https://github.com/scruss/python-hershey) (**CC0/WTFPL**),
  parsed from the comp.sources.unix volume04 Hershey western set.
- `backend/penplot/hershey_fonts.py` (`futural`/`futuram`/`simplex` faces):
  same Hershey acknowledgement; simplex JSON organisation CC0/WTFPL as above;
  futural/futuram data from techninja's
  [hersheytextjs](https://github.com/techninja/hersheytextjs) port of the EMSL
  Hershey engraving fonts (**MIT, Copyright (c) 2014 James T.**).

## 4. Tesseract model

- `backend/penplot/tessdata/eng.traineddata` (4,113,088 bytes): English LSTM
  model from **tessdata_fast** (https://github.com/tesseract-ocr/tessdata_fast),
  **Apache-2.0**. Used by `ocr_text.tesserocr_reader` (technical-drawing OCR,
  digits + `°` whitelist). Vendored so hosts that only install Python
  dependencies (FastAPI Cloud) need no system package or download. Details in
  `backend/penplot/tessdata/README.md`.
- The Tesseract engine itself (libtesseract via the `tesserocr` wheel, or the
  `tesseract` binary for the `pytesseract` fallback) is **not vendored**; it is
  Apache-2.0 upstream (https://github.com/tesseract-ocr/tesseract).

## 5. Deliberately NOT vendored / NOT dependencies

- Potrace (GPL-2.0-or-later) and vpype-flow-imager (GPL-3.0): server-side-only
  per D1, currently neither vendored nor installed; see
  `backend/penplot/methods.py` licence note. If ever added, they stay optional
  server-side extras behind the `LineMethod` protocol.
- System packages: none required (FastAPI Cloud installs Python dependencies
  only); `tesserocr` ships Linux wheels with libtesseract bundled.
