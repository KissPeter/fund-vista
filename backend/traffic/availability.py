"""Cheap "is this day available?" check — one GitHub API call, no archive download.

Port of the ``tools/airport_traffic/availability.py`` POC. Product rule
(owner, 2026-10-11): any UTC day in the last 365 days up to *yesterday* is
selectable; the UI asks this before offering "Load this day". The adsb.lol
release for day D is published after D ends, so today is never selectable.
"""

from __future__ import annotations

import datetime as dt

import httpx

from backend.traffic.ingest import REPO, TAG, UA

WINDOW_DAYS = 365


def selectable_range(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return today - dt.timedelta(days=WINDOW_DAYS), today - dt.timedelta(days=1)


def source_status(day: dt.date, client: httpx.Client, today: dt.date | None = None) -> dict:
    lo, hi = selectable_range(today)
    if not lo <= day <= hi:
        return {"status": "out_of_range", "min": lo.isoformat(), "max": hi.isoformat()}
    tag = TAG.format(y=day.year, m=day.month, d=day.day)
    r = client.get("https://api.github.com/repos/%s/releases/tags/%s" % (REPO.format(year=day.year), tag),
                   headers={"User-Agent": UA})
    if r.status_code == 404:
        return {"status": "unavailable", "reason": "no_release", "tag": tag}
    r.raise_for_status()
    assets = r.json().get("assets", [])
    return {"status": "available", "tag": tag, "bytes": sum(a["size"] for a in assets), "parts": len(assets)}


async def asource_status(day: dt.date, client: httpx.AsyncClient, today: dt.date | None = None) -> dict:
    """Async variant for the FastAPI route (same semantics as :func:`source_status`)."""
    lo, hi = selectable_range(today)
    if not lo <= day <= hi:
        return {"status": "out_of_range", "min": lo.isoformat(), "max": hi.isoformat()}
    tag = TAG.format(y=day.year, m=day.month, d=day.day)
    r = await client.get("https://api.github.com/repos/%s/releases/tags/%s" % (REPO.format(year=day.year), tag),
                         headers={"User-Agent": UA})
    if r.status_code == 404:
        return {"status": "unavailable", "reason": "no_release", "tag": tag}
    r.raise_for_status()
    assets = r.json().get("assets", [])
    return {"status": "available", "tag": tag, "bytes": sum(a["size"] for a in assets), "parts": len(assets)}
