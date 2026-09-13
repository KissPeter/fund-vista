"""City maps from OpenStreetMap (city-roads style) for the pen plotter.

Pipeline: geocode a place name via Nominatim (or take a bbox directly) ->
fetch ways/relations from the Overpass API with per-layer tag filters ->
project to plane meters -> render one ``<g id="citymap-<layer>">`` per
selected layer. The SVG is chrome-free by construction (no location
caption, no attribution comment); :mod:`backend.citymap.chrome` strips
those elements from SVGs exported by the upstream city-roads app.

Raw Overpass payloads and rendered SVGs are cached in Redis
(:mod:`backend.citymap.cache`) so repeat loads never touch OSM servers.
"""

from __future__ import annotations
