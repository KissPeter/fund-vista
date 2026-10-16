# Vendored wordmark font (`backend/wordmark/fonts/`)

Negative-space wordmark renderer (`backend/wordmark/`, `FONTS` in `schemas.py`).
Server-side only — the font never ships to the browser. Generated images carry
no footer/credit (decision D3); attribution lives here, in the ToS/UI and in
API response metadata (`ATTRIBUTION` in `backend/wordmark/schemas.py`).

Licence text next to this file: [`OFL-1.1.txt`](OFL-1.1.txt) (SIL Open Font
License 1.1, verbatim from https://scripts.sil.org/OFL).

| File | Face (API id) | Copyright (embedded name table) | Licence | Upstream source |
|---|---|---|---|---|
| `Poppins-Bold.ttf` | `poppins-bold` | Copyright 2020 The Poppins Project Authors (https://github.com/itfoundry/Poppins); designer records: Indian Type Foundry; Ninad Kale (Devanagari), Jonny Pinhorn (Latin) | SIL OFL 1.1 (nameID 13 + `https://scripts.sil.org/OFL` in nameID 14) | https://github.com/itfoundry/Poppins |

Full third-party inventory (backgrounds, Hershey tables, tessdata, Python
dependencies): `backend/THIRD_PARTY_VENDORED.md` and
`backend/THIRD_PARTY_NOTICES.md`.
