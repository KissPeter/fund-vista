"""Negative-space wordmark generator (``/v1/wordmark``).

Renders a bold wordmark whose letter junctions are pierced by dot cuts —
solid cores floating in carved gap rings. Geometry is a faithful port of
``negspace_logo.py`` (fontTools outlines + skia-pathops booleans); only the
HTTP glue here is new, and it reuses the shared penplot plumbing:

* rate limiting (``require_rate_limit``),
* result URLs (``resolve_public_base`` + cached ``/results/{token}`` SVGs),
* the image registry (``penplot_store.put_image_bytes`` → ``image_id``),
* SVG validation (``imaging.parse_svg_vectors``),
* the cache core (``backend.cachelib`` via the thin ``cache`` binding),
* the ``{"error": {"code", "message"}}`` envelope.

No upstream I/O: rendering is pure local CPU (font outlines + boolean
ops), so results are deterministic per input and hermetic-test friendly.
"""

from __future__ import annotations
