# THIRD_PARTY_NOTICES — fund-vista backend

Third-party components used by the fund-vista backend (issue #58, part 3/7).
Generated images (SVG/plot) carry no footer/credit per decision D3;
attribution lives here, in the ToS, the UI and API response metadata.
Full provenance inventory: `docs/third-party-vendored.md`; font licence
texts: `backend/penplot/fonts/` and `backend/wordmark/fonts/`
(`OFL-1.1.txt`, `MIT.txt`, per-directory `README.md`).

Licence allowlist rule (D4): new dependencies only per the shop repo
`docs/third-party-policy.md`. No AGPL/GPL/LGPL dependency exists in the
backend outside the documented D1 server-side-only exception (Potrace /
vpype-flow-imager — currently neither vendored nor installed).

## A. Vendored files (copyright lines, verbatim from the artefacts)

- `backend/penplot/fonts/Znikoslsvginot8-GOB3y.ttf` —
  "Copyright (c) 2011-2021, gluk (www.glukfonts.pl|gluksza@wp.pl),
  with Reserved Font Names Znikomit and ZnikoSL." — SIL Open Font
  License 1.1 (https://scripts.sil.org/OFL).
- `backend/penplot/fonts/Excalifont-Regular.ttf` —
  "Copyright (c) 2024 by Excalidraw." (font: Jan Filipek / DizajnDesign)
  — SIL Open Font License 1.1 (upstream OFL.txt,
  https://github.com/excalidraw/excalifont).
- `backend/penplot/fonts/ComicShanns-Regular.ttf` —
  "Copyright 2020 Shannon Miwa" — MIT License (permission grant embedded
  in the font; verbatim copy in `backend/penplot/fonts/MIT.txt`).
  NOTE: the only non-OFL font in the backend.
- `backend/penplot/fonts/Nunito-Regular.ttf` —
  "Copyright 2014 The Nunito Project Authors
  (https://github.com/googlefonts/nunito)" (upstream author Vernon Adams)
  — SIL Open Font License 1.1.
- `backend/wordmark/fonts/Poppins-Bold.ttf` —
  "Copyright 2020 The Poppins Project Authors
  (https://github.com/itfoundry/Poppins)" (Indian Type Foundry;
  Ninad Kale, Jonny Pinhorn) — SIL Open Font License 1.1.
- `backend/penplot/tessdata/eng.traineddata` — English LSTM model from
  tessdata_fast (https://github.com/tesseract-ocr/tessdata_fast), Apache License 2.0,
  (c) the Tesseract contributors / Google Inc.
- Hershey stroke tables compiled into `backend/penplot/hershey_font.py`
  and `backend/penplot/hershey_fonts.py` — Hershey Fonts originally created
  by Dr. A. V. Hershey at the U.S. National Bureau of Standards; JSON
  organisation (c) Stewart C. Russell, CC0/WTFPL
  (https://github.com/scruss/python-hershey); futural/futuram port (c) 2014
  James T., MIT (https://github.com/techninja/hersheytextjs).
- `backend/penplot/backgrounds/dark-texture.jpg` and
  `backend/penplot/backgrounds/light-texture.jpg` — provenance recorded in
  `docs/third-party-vendored.md` §2; licence UNKNOWN (replacement is a
  separate, not-yet-made decision).

## B. Python dependencies (`backend/pyproject.toml`)

All permissive. Licence labels verified against the PyPI `license` /
`License :: OSI Approved` metadata; holder names are the upstream projects.

| Package | Licence | Holder / upstream |
|---|---|---|
| fastapi | MIT | (c) Sebastián Ramírez — https://github.com/fastapi/fastapi |
| uvicorn (+ `uvicorn[standard]`) | BSD-3-Clause | (c) Encode — https://github.com/encode/uvicorn |
| starlette (transitive via fastapi) | BSD-3-Clause | (c) Encode — https://github.com/encode/starlette |
| anyio (transitive via starlette) | MIT | (c) Alex Grönholm — https://github.com/agronholm/anyio |
| httpx | BSD-3-Clause | (c) Encode — https://github.com/encode/httpx |
| brotli | MIT | (c) Google — https://github.com/google/brotli |
| redis | MIT | (c) Redis — https://github.com/redis/redis-py |
| pydantic / pydantic-settings | MIT | (c) Pydantic — https://github.com/pydantic/pydantic |
| pillow | HPND (Historical Permission Notice and Disclaimer) | (c) Jeffrey A. Clark and contributors — https://github.com/python-pillow/Pillow |
| numpy | BSD-3-Clause | (c) NumPy Developers — https://github.com/numpy/numpy |
| opencv-python-headless | Apache-2.0 | OpenCV (c) Intel / Itseez; packaging (c) Olli-Pekka Heinisuo — https://github.com/opencv/opencv-python |
| python-multipart | Apache-2.0 | (c) Marcel Kotchy, Andrew Dunham — https://github.com/Kludex/python-multipart |
| defusedxml | PSF (Python Software Foundation License) | (c) Christian Heimes — https://github.com/tiran/defusedxml |
| fonttools | MIT | (c) Just van Rossum, Behdad Esfahbod and contributors — https://github.com/fonttools/fonttools |
| skia-pathops | BSD-3-Clause | (c) the skia-pathops contributors — https://github.com/fonttools/skia-pathops |
| jinja2 | BSD-3-Clause | (c) Pallets — https://github.com/pallets/jinja |
| tesserocr (linux only) | MIT | (c) the tesserocr contributors — https://github.com/sirfz/tesserocr |
| pytesseract | Apache-2.0 | (c) Matthias Lee and contributors — https://github.com/madmaze/pytesseract |
| pytest (test group) | MIT | (c) Holger Krekel and the pytest-dev team — https://github.com/pytest-dev/pytest |

Engines used but NOT vendored and NOT Python dependencies: libtesseract /
`tesseract` binary (Apache-2.0, https://github.com/tesseract-ocr/tesseract)
— bundled in the `tesserocr` Linux wheel or provided by the host.

## C. Deliberately excluded (D1 server-side-only exception)

- Potrace (GPL-2.0-or-later, https://potrace.sourceforge.net) and
  vpype-flow-imager (GPL-3.0, https://github.com/plottertools/vpype-flow-imager):
  importable server-side-only per D1; output (SVG/G-code) is not GPL. Neither
  is vendored nor installed; contour/centerline tracing uses the permissive
  OpenCV/NumPy stack instead (see `backend/penplot/methods.py` licence note).
