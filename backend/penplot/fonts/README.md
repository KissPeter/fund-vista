# Vendored label fonts (`backend/penplot/fonts/`)

Plotter label faces traced or parsed server-side by `backend/penplot/labels.py`
(`_OUTLINE_FILES`) and `backend/penplot/svgfont.py` (`FONT_FILE`). Display and
plot output only — the fonts never ship to the browser. Generated images carry
no footer/credit (decision D3); attribution lives here, in the ToS/UI and in
API response metadata (`ATTRIBUTION_OUTLINE`, `ATTRIBUTION_SINGLESTROKE`).

Licence texts next to this file: [`OFL-1.1.txt`](OFL-1.1.txt) (SIL Open Font
License 1.1, verbatim from https://scripts.sil.org/OFL) and
[`MIT.txt`](MIT.txt) (verbatim permission grant from the font's name table).

| File | Face (UI id) | Copyright (embedded name table) | Licence | Upstream source |
|---|---|---|---|---|
| `Znikoslsvginot8-GOB3y.ttf` | `znikoslsvginot` (default single-stroke) | Copyright (c) 2011-2021, gluk (www.glukfonts.pl), with Reserved Font Names Znikomit and ZnikoSL | SIL OFL 1.1 | https://www.glukfonts.pl — ZnikoSL-SVGinOT-8 by Grzegorz Luka (glukfonts), single-line SVGinOT plotter font |
| `Excalifont-Regular.ttf` | `excalifont` | Copyright (c) 2024 by Excalidraw | SIL OFL 1.1 (upstream `OFL.txt`; the embedded name table carries only the (c) notice, no licence field) | https://github.com/excalidraw/excalifont — Excalifont by Jan Filipek (DizajnDesign) for Excalidraw |
| `ComicShanns-Regular.ttf` | `comic-shanns` | Copyright 2020 Shannon Miwa | **MIT — NOT OFL** (permission grant embedded in nameID 13; see [`MIT.txt`](MIT.txt)) | https://github.com/shannpersand/comic-shanns (Excalidraw build) |
| `Nunito-Regular.ttf` | `nunito` | Copyright 2014 The Nunito Project Authors (upstream author: Vernon Adams) | SIL OFL 1.1 (`https://scripts.sil.org/OFL` in nameID 14) | https://github.com/googlefonts/nunito (Excalidraw weight-500 Latin-subset build) |

Notes:

- **Comic Shanns is the only non-OFL font in this directory** (MIT, permissive —
  no copyleft, no allowlist conflict). Flagged per issue #58: if a face is not
  OFL it must be called out — this is that callout.
- Excalifont's OFL status comes from the upstream repository (`OFL.txt` at the
  excalifont repo root, font by Jan Filipek / DizajnDesign for Excalidraw); the
  vendored TTF's own name table only stores the (c) notice, so the licence file
  next to it is what makes the vendoring compliant.
- Full third-party inventory (backgrounds, Hershey tables, tessdata, Python
  dependencies): `backend/THIRD_PARTY_VENDORED.md` and
  `backend/THIRD_PARTY_NOTICES.md`.
