"""Window statistics for the traffic stats panel (Phase 5, plan §4 item 5).

Pure module: no app import, no network (test-design skill). Computed over
the in-window movements *before* pen filtering, so the numbers are stable
while the user flips toggles — the per-layer ``counts`` from the render
tell the *potential* story (issue #1080: counted before the layer filter),
not the filtered one.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter


def movement_stats(window: list[dict]) -> dict:
    """``{"movements", "arrivals", "departures", "go_arounds",
    "go_around_rate", "holdings", "holding_minutes", "busiest_hour"}``.

    ``go_around_rate`` is go-arounds per movement (0 when empty);
    ``holding_minutes`` sums the classifier's ``holding_s`` spans
    (missing → 0, so pre-c2 movements still work); ``busiest_hour`` is the
    ``"HH:00"`` UTC hour with the most event times (ties → earliest,
    ``None`` when the window is empty).
    """
    moves = list(window)
    go = sum(1 for m in moves if m.get("go_around"))
    holds = sum(1 for m in moves if m.get("holding"))
    hours = Counter(
        dt.datetime.fromtimestamp(m["t"], tz=dt.timezone.utc).strftime("%H:00")
        for m in moves
    )
    busiest = None
    if hours:
        busiest = sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return {
        "movements": len(moves),
        "arrivals": sum(1 for m in moves if m.get("kind") == "arr"),
        "departures": sum(1 for m in moves if m.get("kind") == "dep"),
        "go_arounds": go,
        "go_around_rate": round(go / len(moves), 3) if moves else 0.0,
        "holdings": holds,
        "holding_minutes": int(round(sum(m.get("holding_s") or 0 for m in moves) / 60)),
        "busiest_hour": busiest,
    }


__all__ = ["movement_stats"]
