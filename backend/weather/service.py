"""Weather render service: validation, the L4 render cache, field loads and the
plot-length cap. The HTTP layer (:mod:`backend.weather.router`) only maps
:class:`WeatherError` to the shared error body.

Cache: the composed SVG with its stats and warnings is one JSON value under
``weather_cache_key("render", token)``, where ``token`` is the sha1 of every
render parameter and the renderer version. The same token is the ``svg_url``
suffix, so ``GET /v1/weather/results/{token}`` reads the same entry.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass

from backend.weather import availability as _avail
from backend.weather.cache import cache_get, cache_set, weather_cache_key
from backend.weather.config import (
    ATTRIBUTION,
    CITIES,
    LAYER_FIELDS,
    LAYER_IDS,
    MAX_PLOT_LENGTH_M,
    REGIONS,
)
from backend.weather.fields import FieldReader, load_subset
from backend.weather.geography import Geography, load_geography
from backend.weather.render import compose_artwork

#: Bump when the composed SVG changes meaning, so cached renders expire.
RENDER_VERSION = "r1"

_TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")


class WeatherError(Exception):
    """A request the service refuses; ``status`` and ``code`` go to the client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RenderSpec:
    """A validated render request with canonical (sorted, de-duplicated) lists."""

    date: dt.date
    time: int
    region: str
    orientation: str
    layers: tuple[str, ...]
    cities: tuple[str, ...]

    @classmethod
    def build(cls, *, date: str, time: int, region: str, orientation: str,
              layers: list[str], cities: list[str]) -> "RenderSpec":
        return cls(
            date=dt.date.fromisoformat(date),
            time=time,
            region=region,
            orientation=orientation,
            layers=tuple(name for name in LAYER_IDS if name in set(layers)),
            cities=tuple(name for name in CITIES if name in set(cities)),
        )

    def fields(self) -> frozenset[str]:
        needed: set[str] = set()
        for layer in self.layers:
            needed |= LAYER_FIELDS[layer]
        return frozenset(needed)

    def token(self) -> str:
        """sha1 of every parameter that changes the artwork (plus the renderer version)."""
        payload = {
            "v": RENDER_VERSION,
            "date": self.date.isoformat(),
            "time": self.time,
            "region": self.region,
            "orientation": self.orientation,
            "layers": list(self.layers),
            "cities": list(self.cities),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cache_key(token: str) -> str:
    return weather_cache_key("render", token)


async def render(spec: RenderSpec, *, reader: FieldReader | None = None,
                 geo: Geography | None = None) -> dict:
    """Return ``{"svg", "stats", "warnings", "attribution", "token", "cache_hit"}``.

    Raises :class:`WeatherError` for an out-of-window date (422 ``out_of_range``),
    a failed field read (502 ``weather_source_unavailable``) and a plot longer
    than :data:`MAX_PLOT_LENGTH_M` (422 ``weather_too_long``, never cached).
    """
    token = spec.token()
    cached = await cache_get(_cache_key(token))
    if cached is not None:
        try:
            doc = json.loads(cached)
            return {**doc, "token": token, "cache_hit": True}
        except ValueError:
            pass  # unreadable entry: rebuild it below

    avail = _avail.check(spec.date, spec.time)
    if avail["status"] != "available":
        near = avail["nearest"]
        raise WeatherError(
            422, "out_of_range",
            f"Date {spec.date.isoformat()} {spec.time:02d}:00 UTC is outside the available window; "
            f"nearest is {near['date']} {near['time']:02d}:00 UTC.",
        )

    region = REGIONS[spec.region]
    when = dt.datetime.combine(spec.date, dt.time(spec.time))
    needed = spec.fields()
    fields: dict = {}
    grid = None
    if needed:
        try:
            sub = await load_subset(when, region, reader, needed)
        except ValueError as exc:
            raise WeatherError(422, "out_of_range", str(exc)) from exc
        except Exception as exc:
            raise WeatherError(502, "weather_source_unavailable",
                               f"Weather field read failed: {exc}") from exc
        fields = {name: sub.field(name) for name in needed}
        grid = (sub.lat, sub.lon)

    base = geo if geo is not None else load_geography()
    artwork = await asyncio.to_thread(
        compose_artwork,
        region=region, orientation=spec.orientation, layers=spec.layers,
        cities=spec.cities, geo=base.clipped(region), grid=grid, fields=fields,
    )
    total = artwork.stats["total"]
    length_m = total["mm"] / 1000.0
    if length_m > MAX_PLOT_LENGTH_M:
        raise WeatherError(
            422, "weather_too_long",
            f"This map would plot {length_m:.1f} m of line ({total['minutes']:.1f} min); "
            f"the limit is {MAX_PLOT_LENGTH_M:.0f} m. Turn off a layer (isobars, Z500 or "
            "isotherms) or use fewer cities.",
        )
    doc = {
        "svg": artwork.svg,
        "stats": artwork.stats,
        "warnings": artwork.warnings,
        "attribution": ATTRIBUTION,
    }
    await cache_set(_cache_key(token), json.dumps(doc, separators=(",", ":")))
    return {**doc, "token": token, "cache_hit": False}


async def load_result_svg(token: str) -> str | None:
    """The cached SVG for a render token, or None when unknown or expired."""
    if not _TOKEN_RE.match(token):
        return None
    cached = await cache_get(_cache_key(token))
    if cached is None:
        return None
    try:
        return json.loads(cached)["svg"]
    except (ValueError, KeyError):
        return None
