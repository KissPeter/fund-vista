"""Local OSM extract served as a tiny Overpass-compatible API.

The public Overpass mirrors are the slow, flaky part of ``/v1/citymap``.
This package answers the *exact* query shape citymap sends
(:func:`backend.citymap.overpass.wrap_query`) from a SQLite file built out
of a regional OSM extract (Geofabrik ``hungary-latest.osm.pbf``, ~1-2 GB on
disk). It is listed first in the citymap mirror walk; a bbox outside the
extract, or any query it does not understand, is answered ``400`` and the
walk falls through to the public mirrors.

* ``build``    - PBF (+ optional ``.poly``) -> SQLite (needs ``osmium``)
* ``server``   - ``POST /api/interpreter`` + ``GET /health`` (FastAPI)
* ``store``    - schema, writer, reader (stdlib sqlite3 only)
* ``ql``       - strict parser for the citymap Overpass QL envelope
* ``selectors``/``coverage`` - tag filters and the extract polygon
"""
