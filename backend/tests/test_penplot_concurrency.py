"""Event-loop responsiveness during converts (review D.3.1).

Guards the one-line guarantee: ``POST /v1/convert`` must offload the fully
synchronous, GIL-bound ``run_convert`` to a worker thread via
``asyncio.to_thread``. While a convert is blocked in a stub, a concurrent
``/healthz`` must answer promptly instead of queuing behind it.

In-process (TestClient over a minimal app) because the blocker has to be
injectable; the HTTP-level suite already covers the real stack. If each
request were served on its own event loop this test would degrade to always-
passing (the blocking bug only shows when requests share a loop), but it can
never fail spuriously — the healthz margin is generous and the stub releases
explicitly.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

# Fresh env BEFORE the first backend.penplot import so the router singleton
# store points at a throwaway dir, never the repo's backend/.data.
os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-conc-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"  # hermetic: memory limiter
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

from fastapi import FastAPI, Response  # noqa: E402

from backend.penplot import router as rmod  # noqa: E402
from backend.penplot.errors import PenPlotError  # noqa: E402
from backend.penplot.pipeline import ConvertResult  # noqa: E402
from backend.penplot.schemas import ConvertStats, PointsStats, SegmentsStats  # noqa: E402
from backend.penplot.store import ImageStore  # noqa: E402
from backend.tests.helpers import png_bytes  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


def _result_stub() -> ConvertResult:
    stats = ConvertStats(
        points=PointsStats(before=1, after=1),
        segments=SegmentsStats(before=1, after=1),
        strokes=1, pen_down_mm=1.0, pen_up_mm=0.0, estimated_time_s=0.1,
    )
    return ConvertResult(
        svg_text=(
            '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
            '<path d="M 0 0 L 1 1"/></svg>'
        ),
        filename="0" * 64 + "_" + "0" * 12 + "_optimized.svg",
        stats=stats, warnings=[], vpype_command="vpype -h",
    )


def test_convert_does_not_block_event_loop(tmp_path, monkeypatch):
    store = ImageStore(
        images_dir=str(tmp_path / "images"), results_dir=str(tmp_path / "results")
    )
    monkeypatch.setattr(rmod, "store", store)
    image_id = store.put_image_bytes(png_bytes(), "png")

    started = threading.Event()
    release = threading.Event()

    def slow_run_convert(**kwargs):
        started.set()
        assert release.wait(timeout=10)
        return _result_stub()

    monkeypatch.setattr(rmod, "run_convert", slow_run_convert)

    app = FastAPI()

    @app.get("/healthz")
    async def health() -> Response:
        return Response(status_code=200)

    app.include_router(rmod.router)
    app.add_exception_handler(PenPlotError, rmod.penplot_error_handler)

    convert_responses = []

    def block_on_convert(client: TestClient):
        convert_responses.append(
            client.post(
                "/v1/convert",
                json={"image_id": image_id, "params": {}},
            )
        )

    with TestClient(app) as client:
        # Both threads share this one client, so both requests land on the
        # client's single portal event loop — the regression this guards is
        # only observable when convert and healthz share a loop.
        conv = threading.Thread(target=block_on_convert, args=(client,))
        conv.start()
        assert started.wait(timeout=10), "convert never reached run_convert"

        t0 = time.perf_counter()
        resp = client.get("/healthz")
        elapsed = time.perf_counter() - t0

        assert resp.status_code == 200
        assert elapsed < 1.0, (
            f"event loop was blocked for {elapsed:.2f}s during a convert; "
            "run_convert must run via asyncio.to_thread (D.3.1)"
        )

        release.set()
        conv.join(timeout=10)
        assert not conv.is_alive(), "convert thread hung after release"

    assert len(convert_responses) == 1
    assert convert_responses[0].status_code == 200, convert_responses[0].text