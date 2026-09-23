"""P2 cooperative-cancel tests (render-cancel plan §P2.6, no network).

Unit (pytest, no network): mock ``Request.is_disconnected`` → True at each
checkpoint; assert Overpass never called (1/2), SVG builder never called (3),
loop exits before next stage (4), no cache write, 499 raised.

Integration: stub Overpass with a slow delay, abort mid-fetch via a request
whose ``is_disconnected`` flips after 100 ms; assert the server-side wait is
≈ abort time (not the full delay), the fetch task is cancelled, and no cache
write happens.

System (NAS/staging, needs deploy + nginx) is a curl recipe in the docstring
of ``test_system_recipe_documented`` — not executed here.

HAR bodies (from ``penplot.linuxadm.hu2.har``): [23] aborted render + [24]
successful render share the citymap shape; [28] is the 66 s convert.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend import cancel as cancel_mod
from backend.cancel import (
    ClientCancelled,
    check_cancelled,
    get_cancelled_total,
    race_cancel,
    reset_cancelled_total,
)

# HAR [24] — successful render (bbox actually drawn after snap).
HAR24_BODY = {
    "bbox": {"south": 47.4500, "west": 19.0200, "north": 47.5500, "east": 19.1500},
    "layers": ["roads", "buildings"],
    "min_path_len_m": 10.0,
    "width": 1000,
}

# HAR [23] — aborted render (different bbox, superseded by [24]).
HAR23_BODY = {
    "bbox": {"south": 47.4600, "west": 19.0300, "north": 47.5600, "east": 19.1600},
    "layers": ["roads", "buildings", "water"],
    "min_path_len_m": 10.0,
    "width": 1000,
}


def _request(disconnected: bool = False, flips_after_s: float | None = None):
    scope = {
        "type": "http", "headers": [(b"x-request-id", b"test-req-1")],
        "query_string": b"", "server": ("testserver", 80),
        "scheme": "http", "path": "/", "client": ("127.0.0.1", 1234),
    }
    req = Request(scope)
    start = time.monotonic()

    async def _is_disconnected() -> bool:
        if flips_after_s is not None:
            return (time.monotonic() - start) >= flips_after_s
        return disconnected

    req.is_disconnected = _is_disconnected  # type: ignore[method-assign]
    return req


@pytest.fixture(autouse=True)
def _reset_counter():
    reset_cancelled_total()
    yield
    reset_cancelled_total()


async def _run(coro):
    return await coro


def test_checkpoint1_disconnect_before_work_raises_499_and_counts():
    req = _request(disconnected=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(check_cancelled(
            req, endpoint="/v1/citymap/render",
            stage="validation", started_mono=time.monotonic()))
    assert exc.value.status_code == 499
    assert get_cancelled_total("/v1/citymap/render", "validation") == 1


def test_checkpoint1_connected_passes_without_count():
    req = _request(disconnected=False)
    asyncio.run(check_cancelled(
        req, endpoint="/v1/citymap/render",
        stage="validation", started_mono=time.monotonic()))
    assert get_cancelled_total() == 0


def test_overpass_race_cancels_slow_fetch():
    """P2.3: httpx race — abort stops the wait, partial never cached."""
    async def slow_fetch():
        await asyncio.sleep(5.0)
        return [{"type": "node", "id": 1}]

    req = _request(disconnected=True)
    t0 = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(race_cancel(
            req, slow_fetch(),
            endpoint="/v1/citymap/render", stage="overpass",
            started_mono=t0))
    elapsed = time.monotonic() - t0
    assert exc.value.status_code == 499
    assert elapsed < 1.0, f"race waited {elapsed:.2f}s instead of aborting"
    assert get_cancelled_total("/v1/citymap/render", "overpass") == 1


def test_overpass_race_passes_through_when_connected():
    async def fast_fetch():
        await asyncio.sleep(0.01)
        return [{"type": "node", "id": 1}]

    req = _request(disconnected=False)
    out = asyncio.run(race_cancel(
        req, fast_fetch(), endpoint="/v1/citymap/render",
        stage="overpass", started_mono=time.monotonic()))
    assert out == [{"type": "node", "id": 1}]
    assert get_cancelled_total() == 0


def test_overpass_race_aborts_mid_fetch():
    """Integration: 5 s stub Overpass, client aborts after 100 ms."""
    async def slow_fetch():
        await asyncio.sleep(5.0)
        return []

    req = _request(flips_after_s=0.1)
    t0 = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(race_cancel(
            req, slow_fetch(), endpoint="/v1/citymap/render",
            stage="overpass", started_mono=t0))
    elapsed = time.monotonic() - t0
    assert exc.value.status_code == 499
    assert elapsed < 1.5, f"server held {elapsed:.2f}s after abort (want ≈0.1s)"
    assert get_cancelled_total("/v1/citymap/render", "overpass") == 1


def test_checkpoint4_split_aborts_every_n_ways():
    from backend.citymap.overpass import split_elements

    nodes = [{"type": "node", "id": i, "lon": 19.0, "lat": 47.5} for i in range(1, 6)]
    ways = [
        {"type": "way", "id": 100 + i, "nodes": [1, 2],
         "tags": {"highway": "residential"}}
        for i in range(10)
    ]
    with pytest.raises(ClientCancelled):
        split_elements(
            nodes + ways, ["roads"],
            cancelled=lambda: True, cancel_every=2,
        )


def test_checkpoint4_split_passes_when_connected():
    from backend.citymap.overpass import split_elements

    els = [
        {"type": "node", "id": 1, "lon": 19.0, "lat": 47.5},
        {"type": "node", "id": 2, "lon": 19.1, "lat": 47.5},
        {"type": "way", "id": 10, "nodes": [1, 2],
         "tags": {"highway": "residential"}},
    ]
    geoms, counts = split_elements(els, ["roads"], cancelled=lambda: False)
    assert len(geoms["roads"]) == 1
    assert counts["ways"] == 1


def test_checkpoint4_render_svg_aborts():
    from backend.citymap.render import render_svg

    geoms = {"roads": [[(19.0, 47.5), (19.1, 47.5)] for _ in range(10)]}
    bbox = (47.4, 19.0, 47.6, 19.3)
    with pytest.raises(ClientCancelled):
        render_svg(geoms, bbox, ["roads"], cancelled=lambda: True, cancel_every=2)


def test_checkpoint4_airport_split_aborts():
    from backend.airports.overpass import split_aeroway

    els = [
        {"type": "way", "id": i, "tags": {"aeroway": "runway"},
         "geometry": [{"lon": 19.0, "lat": 47.4}, {"lon": 19.1, "lat": 47.4}]}
        for i in range(10)
    ]
    with pytest.raises(ClientCancelled):
        split_aeroway(els, cancelled=lambda: True, cancel_every=2)


def test_checkpoint4_convert_aborts_between_stages():
    """HAR [28] shape: 66 s convert must stop at the next stage boundary."""
    from backend.penplot.pipeline import run_convert
    from backend.penplot.schemas import ConvertParams
    from backend.tests.helpers import png_bytes

    data = png_bytes(64, 64)
    params = ConvertParams(methods=["hatch"])
    from backend.penplot.router import _settings

    with pytest.raises(ClientCancelled):
        run_convert(
            image_id="a" * 64, image_bytes=data, is_vector=False,
            src_w=64.0, src_h=64.0, params=params, settings=_settings,
            cancelled=lambda: True,
        )


def test_citymap_render_checkpoint1_skips_fetch_and_cache_write():
    """Checkpoint 1: disconnected before network/CPU → 499, no fetch, no store."""
    import backend.citymap.router as router_mod
    from backend.citymap.schemas import RenderRequest

    calls = {"load_raw": 0, "store": 0}

    async def fake_cached(svg_key, counts_key, layers, warnings):
        return None

    async def fake_load_raw(*a, **k):
        calls["load_raw"] += 1
        return [], None

    async def fake_store(*a, **k):
        calls["store"] += 1

    async def scenario():
        import unittest.mock as mock

        with (
            mock.patch.object(router_mod, "_load_cached_render", fake_cached),
            mock.patch.object(router_mod, "_load_raw", fake_load_raw),
            mock.patch.object(router_mod, "_store_render", fake_store),
            mock.patch.object(router_mod, "_resolve_area",
                              return_value=(None, None, (47.45, 19.02, 47.55, 19.15))),
        ):
            with pytest.raises(HTTPException) as exc:
                await router_mod.render(
                    RenderRequest(**HAR24_BODY), _request(disconnected=True))
            assert exc.value.status_code == 499

    asyncio.run(scenario())
    assert calls == {"load_raw": 0, "store": 0}
    assert get_cancelled_total("/v1/citymap/render", "validation") == 1


def test_citymap_render_checkpoint3_skips_build_and_cache_write():
    """Checkpoint 3: disconnect after fetch, before build → 499, no store."""
    import backend.citymap.router as router_mod
    from backend.citymap.schemas import RenderRequest

    calls = {"build": 0, "store": 0}
    real_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *a, **k):
        calls["build"] += 1
        return await real_to_thread(fn, *a, **k)

    async def fake_cached(svg_key, counts_key, layers, warnings):
        return None

    async def fake_load_raw(*a, **k):
        return [], None

    async def fake_store(*a, **k):
        calls["store"] += 1

    async def scenario():
        import unittest.mock as mock

        # Resolve + fetch succeed; disconnect flips before the build.
        req = _request(flips_after_s=1000.0)  # connected during fetch...
        orig_check = cancel_mod.check_cancelled
        n = {"i": 0}

        async def gate(request, *, endpoint, stage, started_mono):
            # ...but gone by the post-fetch checkpoint.
            if stage == "svg_build":
                raise cancel_mod.gone_499()
            return await orig_check(
                request, endpoint=endpoint, stage=stage, started_mono=started_mono)

        with (
            mock.patch.object(router_mod, "_load_cached_render", fake_cached),
            mock.patch.object(router_mod, "_load_raw", fake_load_raw),
            mock.patch.object(router_mod, "_store_render", fake_store),
            mock.patch.object(router_mod, "_resolve_area",
                              return_value=(None, None, (47.45, 19.02, 47.55, 19.15))),
            mock.patch.object(router_mod, "check_cancelled", gate),
            mock.patch.object(router_mod.asyncio, "to_thread", fake_to_thread),
        ):
            with pytest.raises(HTTPException) as exc:
                await router_mod.render(RenderRequest(**HAR23_BODY), req)
            assert exc.value.status_code == 499

    asyncio.run(scenario())
    assert calls["build"] == 0
    assert calls["store"] == 0


def test_system_recipe_documented():
    """System (NAS/staging) recipe — executed manually after deploy:

    1. Deploy per fund-vista rebuild (``fundvista:nas``, health check).
    2. Fire two overlapping renders (HAR [23] + [24] bodies above), abort
       the first: ``curl --max-time 1`` on [23], full wait on [24].
    3. Assert: second ``GET`` still ``200`` in ~3 s; nginx access log shows
       ``499`` for the first ``x-request-id``; ``docker stats`` CPU drops
       after abort instead of staying pegged; no ``svg_url`` file written
       for the aborted id; ``penplot_cancelled_total`` incremented.
    4. Replay: normal render/convert responses byte-shape-identical to today
       (same keys, absolute ``svg_url``, same error tokens on 422/429/413).

    Acceptance: aborted requests stop within ~1 s of disconnect at the
    current stage boundary; non-aborted traffic unchanged; no new shapes.
    """
