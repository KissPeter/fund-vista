CR-007 done (PR #73, issue #66): the adsb.lol archive download is parallel (parts fetched concurrently, consumed in order), the traffic_ingest job publishes real progress counters into the job document, and the parent's share of the pass is measured (~20 % at 4 workers; its cost is the tar walk). set_progress in backend/jobs/store.py is a compare-and-set so a progress write cannot lose a cancel. Docs: docs/CR-007-parallel-archive-download-and-progress.md.

CR-001 done: /penplot is image-only (citymap section removed); new GET /citymap page (backend/citymap/ui.py + templates/citymap.html) with Nominatim candidate picker, MapLibre preview on OpenFreeMap tiles, layer style filters, load-via-visible-bbox into shared convert pipeline. Shared Jinja partials under backend/penplot/templates/partials/ used by both pages. Suite: 158 passed.

F-006 done (2026-10-27, traffic render r4): backend/traffic/basemap.py gains a
Hungary admin-boundary layer (relation admin_level=2, ISO3166-1=HU + name
union, out geom, L3-cached) riding the Map switch; render.py Projection takes
viewport_aspect (canvas = width x width/aspect, bbox content fit-contain
centred) and render_svg takes min_path_len_m (pieces shorter than N meters
dropped on every layer, meters = scale-independent); router grows the view
bbox to the aspect via expand_view_to_aspect (clamped to the loaded day bbox,
never shrinks, residual letterboxes); RenderRequest.min_path_len_m (0..100)
+ viewport_aspect (0<a<=10), both optional so old bodies validate; L4
fingerprint carries both; RENDER_VERSION r3->r4. Fulfillment unchanged
(traffic_group_ink sends unknown suffixes to the Pen). Shop: the traffic tab
sends aspect = pageAspectRatio(live orientation) + a 0-100 m Detail slider
(default 10 m, conditional wire fields so old cart records replay). Docs:
docs/F-006-traffic-country-border-detail-aspect.md.