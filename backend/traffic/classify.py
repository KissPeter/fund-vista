"""Turn raw per-aircraft traces into airport movements: arrival / departure + holding / go-around.

Pure port of the ``tools/airport_traffic/classify.py`` POC — thresholds and
logic unchanged (plan §8). All thresholds are heuristics on ADS-B traces:

* a trace is split into *legs* at gaps > ``LEG_GAP_S``;
* a leg *arrives* if it ends low (AGL < ``LOW_AGL_FT``) within ``NEAR_KM`` of the airport,
  and *departs* if it starts there (a leg doing both is a touch-and-go / circuit and skipped);
* **holding**: >= ``HOLD_TURN_DEG`` cumulative same-direction turn within ``HOLD_WINDOW_S``,
  measured outside the final ``HOLD_EXCLUDE_KM`` (the base/final turns must not count);
* **go-around**: before touching down, the aircraft is below ``GA_MAX_AGL_FT`` within ``GA_KM``
  of the airport and then climbs > ``GA_CLIMB_FT`` within ``GA_WINDOW_S``.
"""

from __future__ import annotations

from typing import Iterable

from backend.traffic.geo import bearing, km_between

LEG_GAP_S = 1200
MIN_LEG_POINTS = 8
NEAR_KM = 15.0
LOW_AGL_FT = 2500
HOLD_TURN_DEG = 330.0
HOLD_WINDOW_S = 720
HOLD_EXCLUDE_KM = 12.0
GA_MAX_AGL_FT = 2000
GA_KM = 10.0
GA_CLIMB_FT = 1200
GA_WINDOW_S = 600

#: Bump when heuristics change — part of the L2 movements cache key so a fix
#: recomputes from L1 without a new 3.5 GB download (plan §6.3).
#: c2 (Phase 5): movements gain ``flow`` (runway direction) + ``holding_s``.
#: c3: traces with ``null`` altitudes no longer crash the classifier (the
#:     altitude is unknown, so it proves neither "low" nor "high").
CLASSIFIER_VERSION = "c3"

# point layout: [ts, lat, lon, alt_ft, ground]
T, LAT, LON, ALT, GND = range(5)


def _agl(p: list, elev_ft: float) -> float | None:
    """Height above the field in feet, or ``None`` when the altitude is unknown.

    Real adsb.lol traces carry ``null`` altitudes for some cruise points.
    An unknown altitude must not be read as "low" (that would invent
    movements) and must not crash the classifier.
    """
    if p[GND]:
        return 0.0
    alt = p[ALT]
    if alt is None:
        return None
    return alt - elev_ft


def legs(pts: list[list]) -> Iterable[list[list]]:
    leg: list[list] = []
    for p in pts:
        if leg and p[T] - leg[-1][T] > LEG_GAP_S:
            yield leg
            leg = []
        leg.append(p)
    if leg:
        yield leg


def _best_turn_window(leg: list[list], lat: float, lon: float) -> tuple[float, float]:
    """``(max cumulative turn deg, span s of that window)`` outside the circuit.

    Shared scan behind :func:`max_turn_deg` and :func:`holding_span_s` so
    both agree on what counts as a hold (plan §4 item 5 needs the span).
    """
    pts = [p for p in leg if km_between(p[LAT], p[LON], lat, lon) > HOLD_EXCLUDE_KM]
    if len(pts) < 10:
        return 0.0, 0.0
    hs = [bearing((pts[i][LAT], pts[i][LON]), (pts[i + 1][LAT], pts[i + 1][LON])) for i in range(len(pts) - 1)]
    cum = [0.0]
    for i in range(1, len(hs)):
        cum.append(cum[-1] + (hs[i] - hs[i - 1] + 180) % 360 - 180)
    best, span = 0.0, 0.0
    for i in range(len(cum)):
        for j in range(i, len(cum)):
            if pts[j][T] - pts[i][T] > HOLD_WINDOW_S:
                break
            turn = abs(cum[j] - cum[i])
            if turn > best:
                best, span = turn, pts[j][T] - pts[i][T]
    return best, span


def max_turn_deg(leg: list[list], lat: float, lon: float) -> float:
    return _best_turn_window(leg, lat, lon)[0]


def holding_span_s(leg: list[list], lat: float, lon: float) -> float:
    """Seconds spent in the max-turn window, or 0 when it is not a hold."""
    turn, span = _best_turn_window(leg, lat, lon)
    return span if turn >= HOLD_TURN_DEG else 0.0


def runway_flow(leg: list[list], kind: str) -> str:
    """Runway direction in use as a ``"%02d"`` number (plan §4 item 3).

    Arrival → true heading of the touchdown segment (last airborne point
    to first ground point); departure → heading of the lift-off segment.
    Rounded to the nearest 10° by runway-number convention
    (``int((h + 5) // 10)``, 0 → 36), so LHBP 13/31 falls out naturally —
    no per-airport runway table needed. Legs without a ground transition
    (trace starts/stops just off the field) fall back to their end segment.
    """
    gnd = [i for i, p in enumerate(leg) if p[GND]]
    if kind == "arr":
        g = next(iter(gnd), None)
        a, b = (leg[g - 1], leg[g]) if g is not None and g > 0 else (leg[-2], leg[-1])
    else:
        air = next((i for i, p in enumerate(leg) if not p[GND]), None)
        a, b = (leg[air - 1], leg[air]) if air is not None and air > 0 else (leg[0], leg[1])
    if a[LAT] == b[LAT] and a[LON] == b[LON]:
        a, b = (leg[-2], leg[-1]) if kind == "arr" else (leg[0], leg[1])
    heading = bearing((a[LAT], a[LON]), (b[LAT], b[LON]))
    return "%02d" % (int((heading + 5) // 10) % 36 or 36)


def has_go_around(leg: list[list], lat: float, lon: float, elev_ft: float) -> bool:
    touch = next((i for i, p in enumerate(leg) if p[GND] and km_between(p[LAT], p[LON], lat, lon) < 5), len(leg))
    for i in range(touch):
        p = leg[i]
        agl = _agl(p, elev_ft)
        if agl is None:
            continue  # unknown altitude — no evidence either way
        if 0 < agl < GA_MAX_AGL_FT and km_between(p[LAT], p[LON], lat, lon) < GA_KM:
            for j in range(i + 1, touch):
                q = leg[j]
                if q[T] - p[T] > GA_WINDOW_S:
                    break
                q_agl = _agl(q, elev_ft)
                if q_agl is not None and q_agl > agl + GA_CLIMB_FT:
                    return True
    return False


def _is_low(p: list, elev_ft: float) -> bool:
    """True when the point is on the ground or provably low above the field."""
    if p[GND]:
        return True
    agl = _agl(p, elev_ft)
    return agl is not None and agl < LOW_AGL_FT


def _kind(leg: list[list], lat: float, lon: float, elev_ft: float) -> str | None:
    def low_near(p: list) -> bool:
        return km_between(p[LAT], p[LON], lat, lon) < NEAR_KM and _is_low(p, elev_ft)

    arr, dep = low_near(leg[-1]), low_near(leg[0])
    if arr and dep:
        return None
    if arr:
        return "arr"
    if dep:
        return "dep"
    # trace may stop/start a little off the airport: use the closest low point
    best = min(range(len(leg)), key=lambda k: km_between(leg[k][LAT], leg[k][LON], lat, lon)
               + (0 if _is_low(leg[k], elev_ft) else 999))
    if not low_near(leg[best]) or km_between(leg[best][LAT], leg[best][LON], lat, lon) > 8:
        return None
    return "arr" if best > len(leg) / 2 else "dep"


def _callsign(calls: list, ts: float) -> str:
    cs = ""
    for t, name in calls:
        if t <= ts:
            cs = name
        else:
            break
    return cs or (calls[0][1] if calls else "")


def classify_day(data: dict) -> list[dict]:
    """All airport movements in a cached day dataset, sorted by airport event time."""
    ap = data["airport"]
    lat, lon, elev = ap["lat"], ap["lon"], ap.get("elev_ft", 0.0)
    out: list[dict] = []
    for ac in data["flights"]:
        for leg in legs(ac["pts"]):
            if len(leg) < MIN_LEG_POINTS:
                continue
            kind = _kind(leg, lat, lon, elev)
            if kind is None:
                continue
            arr = kind == "arr"
            ev = leg[-1][T] if arr else leg[0][T]
            out.append({
                "hex": ac["hex"], "reg": ac.get("reg"), "type": ac.get("type"), "desc": ac.get("desc"),
                "operator": ac.get("operator"), "db_flags": ac.get("db_flags", 0), "flight": _callsign(ac.get("calls") or [], ev),
                "kind": kind, "t": ev,
                "holding": arr and max_turn_deg(leg, lat, lon) >= HOLD_TURN_DEG,
                "holding_s": int(round(holding_span_s(leg, lat, lon))) if arr else 0,
                "flow": runway_flow(leg, kind),
                "go_around": arr and has_go_around(leg, lat, lon, elev),
                "pts": leg,
            })
    out.sort(key=lambda f: f["t"])
    return out
