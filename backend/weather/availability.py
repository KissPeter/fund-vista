"""Pure availability rule for the weather map: date and time bounds.

No network. ``available`` means the ERA5 analysis for that UTC hour is inside
the trusted window (1940-01-01 .. today minus the ERA5T latency) and the hour
is one of the offered analysis times. Anything else is ``out_of_range`` with
the nearest valid date and time, so the UI can offer a one-click fix.
"""

from __future__ import annotations

import datetime as dt

from backend.weather.config import ALLOWED_HOURS, EARLIEST_DATE, LATENCY_DAYS


def latest_date(today: dt.date | None = None) -> dt.date:
    """Newest selectable day: today minus the ERA5T latency."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return today - dt.timedelta(days=LATENCY_DAYS)


def nearest_hour(hour: int) -> int:
    """The allowed analysis hour closest to ``hour`` (ties go earlier)."""
    return min(ALLOWED_HOURS, key=lambda h: (abs(h - hour), h))


def check(day: dt.date, hour: int, today: dt.date | None = None) -> dict:
    """Return ``{"status": "available"}`` or ``{"status": "out_of_range", "nearest": {...}}``.

    ``nearest`` carries the closest valid ``date`` (ISO) and ``time`` (hour,
    int). An hour outside the offered set moves to the nearest offered hour
    on the same day; a date outside the window moves to the nearest edge of
    the window, keeping the hour rule applied.
    """
    latest = latest_date(today)
    date_ok = EARLIEST_DATE <= day <= latest
    hour_ok = hour in ALLOWED_HOURS
    if date_ok and hour_ok:
        return {"status": "available"}
    nearest_day = min(max(day, EARLIEST_DATE), latest)
    return {
        "status": "out_of_range",
        "nearest": {"date": nearest_day.isoformat(), "time": nearest_hour(hour)},
    }
