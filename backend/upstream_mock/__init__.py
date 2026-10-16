"""Mock upstream mode: every third-party HTTP call is served from fixtures.

Enabled with ``FUNDVISTA_UPSTREAM_MODE=mock`` (read once at import of
``backend.main``). Nothing else changes between a normal and a mock run —
routers, parsers, caches and renderers are the production code; only the
network edge is swapped.

How it works: :func:`install` wraps ``httpx.Client`` / ``httpx.AsyncClient``
construction so every client that was not handed an explicit ``transport``
gets :func:`handle` as its transport. All third-party calls in this repo
build a plain httpx client (Overpass, OSM API, Nominatim, OurAirports,
GitHub release index + archive download), so one seam covers them all.

Fail closed: a request whose host/path has no fixture raises
:class:`UpstreamNotMocked` (a ``RuntimeError``, deliberately *not* an
``httpx.HTTPError`` so the callers' ``except httpx.HTTPError`` handlers
cannot swallow it into a quiet 502). A new third-party dependency therefore
fails the hermetic suite loudly instead of reaching the internet.
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import parse_qs

import httpx

from backend.upstream_mock import data

log = logging.getLogger(__name__)

ENV_VAR = "FUNDVISTA_UPSTREAM_MODE"
MOCK = "mock"

_installed = False
_ORIGINAL_INITS: dict[type, object] = {}


class UpstreamNotMocked(RuntimeError):
    """A request reached the network edge in mock mode without a fixture."""


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() == MOCK


_BBOX_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\s*\)")
_AROUND_RE = re.compile(r"\(around:\d+,(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)")
_RELEASE_RE = re.compile(r"^/repos/(adsblol/globe_history_\d{4})/releases/tags/([^/]+)$")
_DOWNLOAD_RE = re.compile(r"^/adsblol/globe_history_\d{4}/releases/download/[^/]+/[^/]+\.tar$")
_OURAIRPORTS_RE = re.compile(r"^/davidmegginson/ourairports-data/main/([a-z-]+)\.csv$")

# Overpass selector fragment -> citymap layer id.
_LAYER_MARKERS: tuple[tuple[str, str], ...] = (
    ("motorway", "highways"),
    ("secondary", "roads"),
    ("footway", "paths"),
    ('"railway"', "rails"),
    ('"aeroway"', "aeroway"),
    ('"waterway"', "waterway"),
    ('"natural"="water"', "water"),
    ('["water"]', "water"),
    ('["building"]', "buildings"),
)


def _json(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})


def _overpass(request: httpx.Request) -> httpx.Response:
    form = parse_qs(request.content.decode("utf-8"))
    query = (form.get("data") or [""])[0]
    around = _AROUND_RE.search(query)
    if around:  # airports: ``out geom`` around the airport centre
        lat, lon = float(around.group(1)), float(around.group(2))
        if '["aeroway"]' in query:
            return _json({"version": 0.6, "elements": data.airport_geom_elements(lat, lon)})
        return _json({"version": 0.6, "elements": data.airport_context_elements(lat, lon)})
    bboxes = [tuple(float(v) for v in m.groups()) for m in _BBOX_RE.finditer(query)]
    if not bboxes:
        raise UpstreamNotMocked(f"overpass query without a bbox or around clause: {query[:120]!r}")
    layers = {layer for marker, layer in _LAYER_MARKERS if marker in query}
    return _json({"version": 0.6, "elements": data.overpass_bbox_elements(bboxes, layers)})  # type: ignore[arg-type]


def _osm_api(request: httpx.Request) -> httpx.Response:
    raw = request.url.params.get("bbox", "")
    try:
        w, s, e, n = (float(v) for v in raw.split(","))
    except ValueError as exc:
        raise UpstreamNotMocked(f"osm api request with unusable bbox {raw!r}") from exc
    return httpx.Response(200, content=data.osm_api_xml((w, s, e, n)).encode(),
                          headers={"content-type": "application/xml"})


def _nominatim(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    limit = int(params.get("limit", "1") or 1)
    return _json(data.nominatim_results(params.get("q", ""), limit))


def handle(request: httpx.Request) -> httpx.Response:
    """Serve one request from synthetic fixtures or fail closed."""
    host, path = request.url.host, request.url.path
    method = request.method.upper()
    if host.startswith("overpass") and method == "POST" and path == "/api/interpreter":
        return _overpass(request)
    if host == "api.openstreetmap.org" and path == "/api/0.6/map":
        return _osm_api(request)
    if host == "nominatim.openstreetmap.org" and path == "/search":
        return _nominatim(request)
    if host == "raw.githubusercontent.com":
        m = _OURAIRPORTS_RE.match(path)
        text = data.ourairports_csv(m.group(1)) if m else None
        if text is not None:
            return httpx.Response(200, content=text.encode("utf-8"), headers={"content-type": "text/csv; charset=utf-8"})
    if host == "api.github.com":
        m = _RELEASE_RE.match(path)
        if m:
            return _json(data.release_json(m.group(2), m.group(1)))
    if host == "github.com" and _DOWNLOAD_RE.match(path):
        return httpx.Response(200, content=data.trace_tar(), headers={"content-type": "application/x-tar"})
    log.error("upstream.not_mocked %s %s://%s%s", method, request.url.scheme, host, path)
    raise UpstreamNotMocked(
        f"{method} {request.url.scheme}://{host}{path} has no upstream-mock fixture "
        f"({ENV_VAR}={MOCK}); add one in backend/upstream_mock/ instead of reaching the network"
    )


def install() -> None:
    """Route every default-transport httpx client through :func:`handle` (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True
    for cls in (httpx.Client, httpx.AsyncClient):
        _wrap(cls)
    log.warning("upstream mock mode ON: no third-party network access")


def uninstall() -> None:
    """Restore the stock httpx clients (tests only; production never calls this)."""
    global _installed
    for cls, original in _ORIGINAL_INITS.items():
        cls.__init__ = original  # type: ignore[method-assign]
    _ORIGINAL_INITS.clear()
    _installed = False


def _wrap(cls: type) -> None:
    original = cls.__init__
    _ORIGINAL_INITS[cls] = original

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "transport" not in kwargs and "mounts" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(handle)
        original(self, *args, **kwargs)

    cls.__init__ = __init__  # type: ignore[method-assign]


def install_if_enabled() -> None:
    if enabled():
        install()
