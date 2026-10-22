"""Catch-all proxy ``/{prefix}/{path}`` live-boundary tests (hermetic).

`GET /api/... | /ersteapi/... | /fxapi/...` over **real HTTP** (in-process
uvicorn on a socket — never TestClient, per the test-design skill) with the
proxy's upstream client stubbed as ``backend.main.http_client = httpx.
AsyncClient(transport=httpx.MockTransport(handler))`` via monkeypatch (the
proxy reads the module global at call time), and Redis forced to the
``None`` state (dead ``REDIS_CLOUD_URL``) or a minimal fake ``await .get`` /
``await .setex`` object. No network, no second server process.

Same process ⇒ `monkeypatch` stubs apply to the serving app; the socket
round-trip still exercises the full stack (routing, both middlewares,
Response building, error mapping).

Contract facts these tests pin down (read off ``backend/main.py``):
* unknown prefix → 404 ``{"detail": "Unknown upstream prefix"}`` (exactly
  one path segment before the catch-all ``path:path``; case-sensitive);
* forward = same method, ``{UPSTREAMS[prefix]}/{path}`` + original query
  string, body bytes verbatim, request headers minus
  ``host/content-length/connection/accept-encoding``;
* upstream passthrough = same status, same body, headers filtered through
  ``_response_headers`` → only ``content-type`` + ``cache-control`` pass
  through, **plus** ``x-cache: MISS`` on every passthrough (even for non-
  cacheable methods — the header is always stamped); both middlewares stamp
  ``x-request-id`` on top;
* cache (only when ``redis_client`` is set and method in
  ``CACHEABLE_METHODS == {"GET", "POST"}``): read-before-send keyed by
  ``fund-vista-proxy:v2:{prefix}:{METHOD}:{path}:{query}:{sha256(body)}``
  (sha256 of b"" for bodyless GET), write-after only for upstream 200
  (``setex`` TTL = seconds until end of local day, 1..86400), payload
  ``{"status", "headers", "body": base64}``; a cached hit replays status +
  stored headers with ``x-cache: HIT`` and never touches the upstream;
* ``httpx.RequestError`` → 502 ``{"detail": "Upstream request failed: …"}``
  (not a bare 500);
* ``GET /healthz`` → 200 ``{"status": "ok", "outbound_tls": "certifi"}``
  when certifi reads (degraded ``fallback:`` / ``unreadable:`` variants are
  unit-covered in ``test_tls_cabundle.py`` — the in-process server always
  has a readable certifi, so only the happy shape is observable over HTTP).
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-proxy-api-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import base64
import hashlib
import json

import httpx
import pytest
import uvicorn
from redis.exceptions import RedisError


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live():
    """Real HTTP: in-process uvicorn on a socket (module scope, one server)."""
    from backend.main import app as full_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        full_app, host="127.0.0.1", port=port,
        log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30.0
    with httpx.Client(timeout=2.0) as probe:
        while time.time() < deadline:
            try:
                if probe.get(f"{base}/healthz").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("proxy test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _caching_off(monkeypatch):
    """Every test starts from the Redis-down state (caching disabled)."""
    import backend.main as backend_main

    monkeypatch.setattr(backend_main, "redis_client", None)
    yield


class _FakeRedis:
    """The minimal async interface the proxy uses: ``await .get``/``.setex``."""

    def __init__(self, stored: dict[str, str] | None = None):
        self.stored = {k: v.encode() for k, v in (stored or {}).items()}
        self.gets: list[str] = []
        self.setexes: list[tuple[str, int, str]] = []
        self.deletes: list[str] = []

    async def get(self, key: str):
        self.gets.append(key)
        return self.stored.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.setexes.append((key, int(ttl), value))
        self.stored[key] = value.encode()  # write-through: next get() hits

    async def delete(self, key: str):
        self.deletes.append(key)
        self.stored.pop(key, None)
        return 1


def _stub_upstream(monkeypatch, responder):
    """Swap ``backend.main.http_client`` for a MockTransport client.

    ``responder(request) → httpx.Response`` (or raise ``httpx.RequestError``).
    Returns the list of captured upstream requests.
    """
    import backend.main as backend_main

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(backend_main, "http_client", client)
    return seen


def _echo(request: httpx.Request) -> httpx.Response:
    """Upstream double: answers 200 with a fixed JSON body."""
    return httpx.Response(200, content=b'{"fib": [1, 1, 2, 3, 5, 8]}',
                          headers={"content-type": "application/json",
                                   "cache-control": "max-age=60"})


# -- unknown prefix ----------------------------------------------------------


class TestUnknownPrefix:
    def test_unknown_prefix_404_contract(self, live):
        for prefix in ("nose", "apis", "api2", "API"):  # "API": case-sensitive
            resp = live.get(f"/{prefix}/fib/test")
            assert resp.status_code == 404, (prefix, resp.text)
            assert resp.json() == {"detail": "Unknown upstream prefix"}
            assert resp.headers["content-type"] == "application/json"

    def test_upstreams_are_exactly_the_three_known_prefixes(self):
        import backend.main as backend_main

        assert backend_main.UPSTREAMS == {
            "api": "https://www.kh.hu",
            "ersteapi": "https://www.erstemarket.hu",
            "fxapi": "https://api.frankfurter.dev/v1",
        }
        assert backend_main.CACHEABLE_METHODS == {"GET", "POST"}


# -- happy-path forwarding ---------------------------------------------------


class TestForwardHappyPath:
    def test_each_known_prefix_hits_its_upstream(self, live, monkeypatch):
        import backend.main as backend_main

        for prefix, base in backend_main.UPSTREAMS.items():
            stub = _stub_upstream(monkeypatch, _echo)
            resp = live.get(f"/{prefix}/fib/test")
            assert resp.status_code == 200, resp.text
            assert len(stub) == 1
            assert str(stub[0].url).startswith(f"{base}/fib/test")
            assert stub[0].method == "GET"
            assert resp.json() == {"fib": [1, 1, 2, 3, 5, 8]}

    def test_headers_passthrough_and_hop_by_hop_strip(self, live, monkeypatch):
        stub = _stub_upstream(monkeypatch, _echo)
        sent = live.get("/api/fib/test", headers={
            "x-trace-hop": "from-proxy-client",
            "accept-encoding": "identity",
            "connection": "close",  # stripped; httpx adds its own keep-alive hop
        })
        assert sent.status_code == 200, sent.text
        upstream = stub[0]
        # the client's own hop-by-hop headers did not reach the upstream
        assert upstream.headers.get("accept-encoding") != "identity"
        assert upstream.headers.get("connection") != "close"
        # a plain custom header is forwarded verbatim
        assert upstream.headers["x-trace-hop"] == "from-proxy-client"

        # response side: _response_headers lets content-type/cache-control
        # through, stamps x-cache: MISS; middleware stamps x-request-id.
        assert sent.headers["content-type"] == "application/json"
        assert sent.headers["cache-control"] == "max-age=60"
        assert sent.headers["x-cache"] == "MISS"
        assert sent.headers.get("x-request-id")
        assert sent.content == b'{"fib": [1, 1, 2, 3, 5, 8]}'


# -- cache behavior ----------------------------------------------------------


class TestCacheBehavior:
    def test_two_gets_miss_then_hit(self, live, monkeypatch):
        import backend.main as backend_main
        from backend.main import _cache_key

        stub = _stub_upstream(monkeypatch, _echo)
        fake = _FakeRedis()
        monkeypatch.setattr(backend_main, "redis_client", fake)

        first = live.get("/api/latest")
        assert first.status_code == 200 and first.headers["x-cache"] == "MISS"
        second = live.get("/api/latest")
        assert second.status_code == 200 and second.headers["x-cache"] == "HIT"
        assert second.json() == first.json()

        want_key = _cache_key("api", "latest", "", "GET", b"")
        assert want_key.startswith("fund-vista-proxy:v2:api:GET:latest:")
        assert want_key.endswith(hashlib.sha256(b"").hexdigest())
        assert fake.gets == [want_key, want_key]
        assert len(stub) == 1  # the HIT never re-reached the upstream
        (key, ttl, value), = fake.setexes
        assert key == want_key
        assert 1 <= ttl <= 86400  # seconds until end of day
        payload = json.loads(value)
        assert payload["status"] == 200
        assert payload["headers"]["content-type"] == "application/json"
        assert payload["headers"]["x-cache"] == "MISS"
        assert base64.b64decode(payload["body"]) == first.content

    def test_preseeded_hit_serves_cached_bytes_without_upstream(self, live, monkeypatch):
        import backend.main as backend_main
        from backend.main import _cache_key

        stub = _stub_upstream(monkeypatch, _echo)
        cached_body = b'{"cached": true}'
        payload = {
            "status": 200,
            "headers": {"content-type": "application/json",
                        "cache-control": "no-store", "x-cache": "MISS"},
            "body": base64.b64encode(cached_body).decode("ascii"),
        }
        fake = _FakeRedis(stored={_cache_key("api", "fib/x", "", "GET", b""):
                                  json.dumps(payload)})
        monkeypatch.setattr(backend_main, "redis_client", fake)

        resp = live.get("/api/fib/x")
        assert resp.status_code == 200, resp.text
        assert resp.content == cached_body  # base64-decoded, byte-exact
        assert resp.headers["x-cache"] == "HIT"
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["cache-control"] == "no-store"
        assert stub == []  # stored replay, upstream never consulted

    def test_post_is_cacheable(self, live, monkeypatch):
        import backend.main as backend_main
        from backend.main import _cache_key

        stub = _stub_upstream(monkeypatch, _echo)
        fake = _FakeRedis()
        monkeypatch.setattr(backend_main, "redis_client", fake)
        body = json.dumps({"flag": 1}).encode()

        assert live.post("/api/fib/x", content=body).headers["x-cache"] == "MISS"
        assert live.post("/api/fib/x", content=body).headers["x-cache"] == "HIT"
        assert fake.gets == [_cache_key("api", "fib/x", "", "POST", body),
                             _cache_key("api", "fib/x", "", "POST", body)]
        assert len(stub) == 1

    def test_non_cacheable_methods_bypass_read_and_write(self, live, monkeypatch):
        stub = _stub_upstream(monkeypatch, _echo)
        fake = _FakeRedis()
        import backend.main as backend_main

        monkeypatch.setattr(backend_main, "redis_client", fake)
        for method in ("PUT", "PATCH", "DELETE"):  # outside CACHEABLE_METHODS
            first = live.request(method, "/api/fib/x")
            second = live.request(method, "/api/fib/x")
            assert first.status_code == second.status_code == 200
            assert first.headers["x-cache"] == "MISS"
            assert second.headers["x-cache"] == "MISS"
        assert fake.gets == [] and fake.setexes == []
        assert len(stub) == 6  # every call went to the upstream

    def test_non_200_upstream_is_never_written(self, live, monkeypatch):
        def _created(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, content=b'{"made": true}')

        stub = _stub_upstream(monkeypatch, _created)
        fake = _FakeRedis()
        import backend.main as backend_main

        monkeypatch.setattr(backend_main, "redis_client", fake)
        body = b'{"payload": 2}'
        for _ in range(2):
            resp = live.post("/api/fib/x", content=body)
            assert resp.status_code == 201, resp.text
            assert resp.headers["x-cache"] == "MISS"
        assert fake.setexes == []  # only upstream 200 responses are cached
        assert len(stub) == 2

    def test_corrupt_cache_entry_falls_back_to_upstream(self, live, monkeypatch):
        """A malformed cache entry must not become a 500 (issue #94).

        The poisoned key is deleted and the request falls through to the
        upstream, which answers 200.
        """
        import backend.main as backend_main
        from backend.main import _cache_key

        stub = _stub_upstream(monkeypatch, _echo)
        key = _cache_key("api", "fib/corrupt", "", "GET", b"")
        fake = _FakeRedis(stored={key: "{not json at all"})
        monkeypatch.setattr(backend_main, "redis_client", fake)

        resp = live.get("/api/fib/corrupt")
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-cache"] == "MISS"
        assert resp.json() == {"fib": [1, 1, 2, 3, 5, 8]}
        assert len(stub) == 1  # the upstream was consulted
        assert fake.deletes == [key]  # the poisoned entry was dropped

    def test_corrupt_entry_missing_body_key_falls_back(self, live, monkeypatch):
        """A JSON entry without a "body" key is corrupt too (KeyError path)."""
        import backend.main as backend_main
        from backend.main import _cache_key

        stub = _stub_upstream(monkeypatch, _echo)
        key = _cache_key("api", "fib/nobody", "", "GET", b"")
        fake = _FakeRedis(stored={key: json.dumps({"status": 200, "headers": {}})})
        monkeypatch.setattr(backend_main, "redis_client", fake)

        resp = live.get("/api/fib/nobody")
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-cache"] == "MISS"
        assert len(stub) == 1
        assert fake.deletes == [key]

    def test_redis_error_does_not_disable_caching(self, live, monkeypatch):
        """A transient Redis error is a cache miss, not a permanent outage."""
        import backend.main as backend_main
        from backend.main import _cache_key

        class _FlakyRedis(_FakeRedis):
            def __init__(self):
                super().__init__()
                self.fail_next = True

            async def get(self, key: str):
                self.gets.append(key)
                if self.fail_next:
                    self.fail_next = False
                    raise RedisError("connection reset")
                return self.stored.get(key)

        stub = _stub_upstream(monkeypatch, _echo)
        fake = _FlakyRedis()
        monkeypatch.setattr(backend_main, "redis_client", fake)

        first = live.get("/api/latest")
        assert first.status_code == 200, first.text
        assert first.headers["x-cache"] == "MISS"
        # the client survived the error: the next request still reads the cache
        second = live.get("/api/latest")
        assert second.status_code == 200, second.text
        assert second.headers["x-cache"] == "HIT"
        assert len(stub) == 1
        assert backend_main.redis_client is fake
        assert _cache_key("api", "latest", "", "GET", b"") in fake.gets

    def test_redis_none_skips_cache_entirely(self, live, monkeypatch):
        # _caching_off leaves redis_client = None (the state after a failed
        # connection) → plain passthrough handling, no read/write, no HIT.
        stub = _stub_upstream(monkeypatch, _echo)
        for _ in range(2):
            resp = live.get("/api/fib/x")
            assert resp.status_code == 200
            assert resp.headers["x-cache"] == "MISS"
        assert len(stub) == 2


# -- upstream I/O failure ----------------------------------------------------


class TestUpstreamRequestError:
    def test_connect_error_maps_to_502(self, live, monkeypatch):
        _stub_upstream(monkeypatch, lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused", request=request)))

        resp = live.get("/api/fib/x")
        assert resp.status_code == 502, resp.text  # NOT a bare 500
        assert resp.json() == {"detail": "Upstream request failed: connection refused"}


# -- method + body forwarding ------------------------------------------------


class TestMethodForwarding:
    def test_post_body_and_headers_forwarded(self, live, monkeypatch):
        stub = _stub_upstream(monkeypatch, _echo)
        body = json.dumps({"spec": "probe"}).encode()
        resp = live.post("/api/orders", content=body, headers={
            "content-type": "application/json", "x-idempotency": "tok-7"})
        assert resp.status_code == 200, resp.text
        (upstream,) = stub
        assert upstream.method == "POST"
        assert upstream.content == body  # body bytes verbatim
        assert upstream.headers["x-idempotency"] == "tok-7"

    def test_status_and_all_routed_methods_pass_through(self, live, monkeypatch):
        def _created(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, content=b'{"ok": true}')

        stub = _stub_upstream(monkeypatch, _created)
        resp = live.post("/api/orders", content=b"{}")
        assert resp.status_code == 201  # upstream status passthrough
        assert resp.headers["x-cache"] == "MISS"

        stub = _stub_upstream(monkeypatch, _created)
        for method in ("PUT", "PATCH", "DELETE", "GET"):
            resp = live.request(method, "/api/orders")
            assert resp.status_code == 201, (method, resp.text)
            assert stub[-1].method == method


# -- query string forwarding -------------------------------------------------


class TestQueryForwarding:
    def test_query_params_forwarded(self, live, monkeypatch):
        stub = _stub_upstream(monkeypatch, _echo)
        resp = live.get("/fxapi/latest", params={"a": "1", "b": "two"})
        assert resp.status_code == 200, resp.text
        (upstream,) = stub
        assert str(upstream.url) == "https://api.frankfurter.dev/v1/latest?a=1&b=two"
        assert upstream.url.params["a"] == "1"
        assert upstream.url.params["b"] == "two"


# -- health endpoint ---------------------------------------------------------


class TestHealthzContract:
    def test_healthz_ok_certifi(self, live):
        resp = live.get("/healthz")
        assert resp.status_code == 200, resp.text
        # certifi readable in the test env → happy shape only; the degraded
        # "fallback:..." / "unreadable:..." variants are unit-covered in
        # test_tls_cabundle.py (they cannot be observed over this socket).
        assert resp.json() == {"status": "ok", "outbound_tls": "certifi"}
        assert resp.headers["content-type"] == "application/json"
