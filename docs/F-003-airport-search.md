# F-003 — Freeform + IATA airport search

- Status: done
- Created: 2026-09-14
- Last updated: 2026-09-14
- Type: feature
- Scope: `backend/airports` only (ourairports ranking, one GET route,
  lookup fallback, page picker, tests). No changes to render/import or
  citymap/penplot.

## Motivation

Lookup is ICAO-only; users know city names ("Budapest") and IATA codes
("BUD") more often than ICAO ("LHBP"). Same data (cached `airports.csv`)
answers all three — no new upstream.

## Scope

1. `GET /v1/airports/search?q=bud&limit=5` → ranked candidates
   `{icao, iata, name, municipality, iso_country, lat, lon, type}`,
   mirror of citymap `geocode/search` conventions (`limit` 1–10, default
   5; unthrottled; `{"error":{"code","message"}}` envelope).
2. Ranking (pure function, unit-tested): exact ident/iata/gps match >
   ident/iata prefix > name/municipality substring; airport types
   `large > medium > small > seaplane/heliport`; `closed` rows excluded.
   CSV parse off the event loop (`asyncio.to_thread`, ~12 MB file).
3. `lookup` gains an IATA fallback (ident → gps_code → iata_code), so
   `lookup?icao=BUD` resolves LHBP; response `icao` is the canonical
   OurAirports ident.
4. `/airports` page: freeform box → candidate `<select>` → fills the ICAO
   field (same picker pattern as `/citymap`).

## QA / DoD

- Unit (no network): ranking on synthetic rows (exact IATA beats name
  substring; closed excluded; limit honored).
- HTTP hermetic: missing/short `q` → 422; `limit=99` → 422; page markup
  contains search box + picker + hook.
- Full suite green; commit `feat(airports): …`.
