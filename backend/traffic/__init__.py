"""Airport traffic backend module (plan Phase 1: ingest + classify).

Pure ports of the ``tools/airport_traffic/`` POC (``classify.py``,
``filters.py``, ``geo.py``, ingest streaming) plus the durable slice store
(``store.py``), Redis cache binding (``cache.py``), availability endpoint and
the ``traffic_ingest`` background job. Render/basemap (Phase 2) and the shop
tab UI (Phase 3) are explicitly out of scope here.
"""

from __future__ import annotations
