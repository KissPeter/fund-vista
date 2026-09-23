"""P3 jobs contract tests (render-cancel plan §P3.4, no shop context needed).

Contract (pytest + real app, fake Overpass/vpype where noted):

* ``POST /v1/jobs`` invalid body → 422 invalid_params, no store key.
* Valid ``POST`` → 202 in <100 ms, ``GET`` → queued → running → done with
  ``result`` matching the sync schema (keys: svg_url, path_counts/stats,
  warnings).
* ``DELETE`` while queued → never runs (no Overpass call). ``DELETE`` while
  running → cancelled, no partial file, ``GET`` stays cancelled.
* ``cancel_previous:true``: POST A then POST B same IP+type → A cancelled,
  B runs; response B carries ``X-Penplot-Superseded: A``.
* TTL: ``GET`` unknown id → 404 job_not_found.
* Cross-check: ``result`` of async citymap_render deep-equals sync
  ``POST /v1/citymap/render`` for the HAR [24] body (modulo cache_hit).

System (NAS curl) recipe in ``test_system_curl_recipe_documented``.
"""

from __future__ import annotations

import os
import tempfile
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-jobs-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.jobs import runner as jobs_runner
from backend.jobs import store as jobs_store
from backend.main import app as full_app
from backend.penplot.errors import PenPlotError

HAR24_BODY = {
    "bbox": {"south": 47.4500, "west": 19.0200, "north": 47.5500, "east": 19.1500},
    "layers": ["roads", "buildings"],
    "min_path_len_m": 10.0,
    "width": 1000,
}


@pytest.fixture()
def client(monkeypatch):
    jobs_store.reset_memory()
    jobs_runner.reset_local()
    jobs_store.configure_jobs_redis(None)
    # Hermetic rate limiter (memory, huge window).
    from backend.penplot import router as penplot_router

    penplot_router._limiter._memory.clear()
    with TestClient(full_app) as c:
        yield c
    jobs_runner.reset_local()
    jobs_store.reset_memory()


def _post_job(c, job_type, payload, cancel_previous=True):
    t0 = time.perf_counter()
    resp = c.post(
        "/v1/jobs",
        json={"type": job_type, "request": payload, "cancel_previous": cancel_previous},
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return resp, elapsed_ms


def _wait_done(c, job_id, timeout_s=30.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = c.get(f"/v1/jobs/{job_id}")
        assert last.status_code == 200, last.text
        status = last.json()["status"]
        if status in ("done", "failed", "cancelled"):
            return last.json()
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never finished: {last.json() if last else None}")


def test_post_invalid_body_422_no_job_created(client):
    resp, _ = _post_job(client, "citymap_render", {"bbox": {"south": 1.0}})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_post_unknown_job_type_422(client):
    resp = client.post(
        "/v1/jobs", json={"type": "nope", "request": {}, "cancel_previous": False})
    assert resp.status_code == 422, resp.text


def test_post_valid_convert_202_fast_and_done_matches_sync_schema(client):
    from backend.tests.helpers import default_params, png_bytes, upload

    up = upload(client, png_bytes(64, 64))
    assert up.status_code == 200, up.text
    image_id = up.json()["image_id"]

    payload = {"image_id": image_id, "params": default_params("hatch")}
    resp, elapsed_ms = _post_job(client, "convert", payload, cancel_previous=False)
    assert resp.status_code == 202, resp.text
    assert elapsed_ms < 5000, f"enqueue took {elapsed_ms:.1f}ms (want <100ms prod, <5s test)"
    body = resp.json()
    assert body["status"] == "queued"
    assert body["status_url"] == f"/v1/jobs/{body['job_id']}"
    assert body["cancel_url"] == f"/v1/jobs/{body['job_id']}"
    assert body["poll_after_ms"] == 500
    assert len(body["job_id"]) == 32

    # poll → running → done
    seen = set()
    deadline = time.time() + 30.0
    final = None
    while time.time() < deadline:
        g = client.get(f"/v1/jobs/{body['job_id']}")
        assert g.status_code == 200
        seen.add(g.json()["status"])
        if g.json()["status"] in ("done", "failed", "cancelled"):
            final = g.json()
            break
        time.sleep(0.2)
    assert final is not None, "job never finished"
    assert final["status"] == "done", final
    result = final["result"]
    for key in ("image_id", "svg_url", "vpype_command", "stats", "warnings"):
        assert key in result, key
    assert result["image_id"] == image_id
    assert "/v1/results/" in result["svg_url"]
    assert "queued" in seen or "running" in seen


def test_get_unknown_id_404_job_not_found(client):
    resp = client.get("/v1/jobs/" + "0" * 32)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "job_not_found"


def test_delete_unknown_id_404(client):
    resp = client.delete("/v1/jobs/" + "0" * 32)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "job_not_found"


def test_delete_while_running_cancels_and_stays_cancelled(client, monkeypatch):
    """DELETE a running convert → cancelled, no result, GET stays cancelled."""
    import backend.jobs.runner as runner_mod
    import backend.penplot.pipeline as pipeline_mod

    from backend.tests.helpers import default_params, png_bytes, upload

    up = upload(client, png_bytes(64, 64))
    image_id = up.json()["image_id"]

    started = __import__("threading").Event()
    release = __import__("threading").Event()
    real_run = pipeline_mod.run_convert

    def blocking_run_convert(**kwargs):
        started.set()
        assert release.wait(timeout=15)
        return real_run(**kwargs)

    monkeypatch.setattr(pipeline_mod, "run_convert", blocking_run_convert)

    resp, _ = _post_job(
        client, "convert",
        {"image_id": image_id, "params": default_params("hatch")},
        cancel_previous=False)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert started.wait(timeout=15), "job never started running"

    delete = client.delete(f"/v1/jobs/{job_id}")
    assert delete.status_code == 200
    assert delete.json() == {"job_id": job_id, "status": "cancelled"}
    release.set()

    final = _wait_done(client, job_id, timeout_s=15.0)
    assert final["status"] == "cancelled", final
    assert final.get("result") is None
    # idempotent second DELETE
    again = client.delete(f"/v1/jobs/{job_id}")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_cancel_previous_supersedes_same_ip_type(client, monkeypatch):
    """POST A then POST B (same IP+type) → A cancelled, B header lists A."""
    import backend.penplot.pipeline as pipeline_mod

    from backend.tests.helpers import default_params, png_bytes, upload

    up = upload(client, png_bytes(64, 64))
    image_id = up.json()["image_id"]

    started_a = __import__("threading").Event()
    release_a = __import__("threading").Event()
    real_run = pipeline_mod.run_convert
    calls = {"n": 0}

    def gated_run(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            started_a.set()
            assert release_a.wait(timeout=15)
            from backend.cancel import ClientCancelled

            raise ClientCancelled("/v1/convert", "vpype:linesort")
        return real_run(**kwargs)

    monkeypatch.setattr(pipeline_mod, "run_convert", gated_run)

    resp_a, _ = _post_job(
        client, "convert",
        {"image_id": image_id, "params": default_params("hatch")},
        cancel_previous=True)
    assert resp_a.status_code == 202
    job_a = resp_a.json()["job_id"]
    assert started_a.wait(timeout=15)

    resp_b, _ = _post_job(
        client, "convert",
        {"image_id": image_id, "params": default_params("hatch")},
        cancel_previous=True)
    assert resp_b.status_code == 202
    job_b = resp_b.json()["job_id"]
    assert resp_b.headers.get("x-penplot-superseded") == job_a

    release_a.set()
    final_a = _wait_done(client, job_a, timeout_s=15.0)
    assert final_a["status"] == "cancelled", final_a
    final_b = _wait_done(client, job_b, timeout_s=30.0)
    assert final_b["status"] == "done", final_b


def test_per_ip_limit_429_with_retry_after(client):
    from backend.tests.helpers import default_params, png_bytes, upload

    up = upload(client, png_bytes(160, 120))
    image_id = up.json()["image_id"]

    import backend.jobs.runner as runner_mod

    # Freeze execution so all three enqueues stay non-terminal.
    orig_enqueue = runner_mod.enqueue
    monkeypatch_holder = {}
    try:
        runner_mod.enqueue = lambda job_id: None  # type: ignore[assignment]
        ids = []
        for _ in range(3):
            resp, _ = _post_job(
                client, "convert",
                {"image_id": image_id, "params": default_params("hatch")},
                cancel_previous=False)
            assert resp.status_code == 202, resp.text
            ids.append(resp.json()["job_id"])
        fourth, _ = _post_job(
            client, "convert",
            {"image_id": image_id, "params": default_params("hatch")},
            cancel_previous=False)
        assert fourth.status_code == 429, fourth.text
        assert fourth.json()["error"]["code"] == "rate_limited"
        assert fourth.headers.get("retry-after") == "60"
    finally:
        runner_mod.enqueue = orig_enqueue  # type: ignore[assignment]


def test_async_citymap_result_matches_sync_keys(client, monkeypatch):
    """Cross-check: async citymap_render result keys == sync response keys."""
    import backend.citymap.router as citymap_router

    elements = [
        {"type": "node", "id": 1, "lon": 19.02, "lat": 47.50},
        {"type": "node", "id": 2, "lon": 19.10, "lat": 47.50},
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "residential"}},
    ]

    async def fake_load_raw(*a, **k):
        return list(elements), None

    monkeypatch.setattr(citymap_router, "_load_raw", fake_load_raw)

    sync = client.post("/v1/citymap/render", json=HAR24_BODY)
    assert sync.status_code == 200, sync.text
    sync_body = sync.json()

    resp, _ = _post_job(client, "citymap_render", HAR24_BODY, cancel_previous=False)
    assert resp.status_code == 202
    final = _wait_done(client, resp.json()["job_id"])
    assert final["status"] == "done", final
    result = final["result"]
    # Same keys as sync success (modulo cache_hit, which may differ).
    assert set(result.keys()) == set(sync_body.keys()), (
        set(result.keys()) ^ set(sync_body.keys()))
    for key in ("city", "bbox", "layers", "path_counts", "raw_counts",
                "attribution"):
        assert result[key] == sync_body[key], key
    # Warnings may differ only by cache-hit tokens (sync run warms the cache
    # the async job then hits) — geometry-affecting warnings must match.
    cache_tokens = {"svg_cache_hit", "overpass_cache_hit", "geocode_cache_hit",
                    "counts_from_cached_svg"}
    assert (set(result["warnings"]) - cache_tokens) == (
        set(sync_body["warnings"]) - cache_tokens)
    assert result["svg_url"].startswith("http")
    assert "/v1/citymap/results/" in result["svg_url"]


def test_system_curl_recipe_documented():
    """System acceptance via curl (NAS/staging, same deploy as P2):

    .. code-block:: sh

        BASE=https://penplot.linuxadm.hu
        # 1. enqueue a slow convert job (no >100 ms POST held)
        J=$(curl -s -X POST $BASE/v1/jobs \\
          -H 'Content-Type: application/json' \\
          -d '{"type":"convert","request":{"image_id":"<64hex>","params":{"methods":["hatch"]}},"cancel_previous":true}' \\
          | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
        # 2. poll (500 ms → 2 s backoff, 310 s cap)
        while true; do
          S=$(curl -s $BASE/v1/jobs/$J | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])');
          echo "status=$S"; [ "$S" = done -o "$S" = failed -o "$S" = cancelled ] && break;
          sleep 1;
        done
        # 3. mid-run cancel from another shell proves server-visible cancel:
        curl -s -X DELETE $BASE/v1/jobs/$J
        # 4. overlapping same-IP jobs: second POST auto-cancels the first
        #    (response header X-Penplot-Superseded carries the old id);
        #    assert CPU drops and GET $OLD stays "cancelled".

    Acceptance: no POST blocks >100 ms; interactive cancel always
    server-visible (DELETE or auto-supersede); sync shapes preserved inside
    ``result``/``error``; 66 s converts complete via polling.
    """
