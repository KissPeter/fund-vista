"""Real-HTTP tests for the STL three-view backend (no TestClient).

A module-scoped uvicorn thread + plain ``httpx.Client`` exercise routing,
multipart parsing, validation envelopes and result/meta gates exactly as
production serves them. Only the external boundaries are stubbed: the STL
toolchain load/render functions. The filesystem store is real and points at a
tmp dir. No new dependency and no ``backend/uv.lock`` change.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import numpy as np
import pytest
import uvicorn

from backend.penplot.config import STL_MAX_FACES, STL_MAX_UPLOAD_BYTES, Settings
from backend.penplot.errors import ErrorCode
from backend.penplot.stl_views import GENERATOR_VERSION
from backend.tests.helpers import TEST_HMAC_SECRET, png_bytes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _view_svg(view: str) -> str:
    """Small deterministic artwork-only SVG for one named view."""
    height = {"broad": 90, "edge": 45, "end": 90}[view]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="180mm" '
        f'height="{height}mm" viewBox="0 0 180 {height}">'
        f'<path d="M 0 0 L 180 0 L 180 {height} L 0 {height} Z" '
        'fill="none" stroke="#000000" stroke-width="0.2" /></svg>'
    )


@dataclass
class _Boundary:
    """Stub for the one external toolchain boundary and its call counters."""

    faces: int = 12
    extents: np.ndarray = field(default_factory=lambda: np.array([4.0, 2.0, 1.0]))
    watertight: bool = True
    mode: str = "ok"
    render_calls: int = 0

    class _Mesh:
        def __init__(self, faces: int, extents: np.ndarray, watertight: bool) -> None:
            self.faces = range(faces)
            self.extents = extents
            self.is_watertight = watertight

    def reset(self) -> None:
        self.faces, self.extents, self.watertight = 12, np.array([4.0, 2.0, 1.0]), True
        self.mode, self.render_calls = "ok", 0

    def load(self, data: bytes):
        if self.mode == "missing":
            raise ModuleNotFoundError("No module named 'slice3d'")
        if self.mode == "bad":
            from backend.penplot.stl_views import StlFormatError

            raise StlFormatError("stub: bad mesh")
        return self._Mesh(self.faces, self.extents, self.watertight)

    def render(self, data: bytes, views: list[str], params):
        if self.mode == "missing":
            raise ModuleNotFoundError("No module named 'slice3d'")
        if self.mode == "bad":
            from backend.penplot.stl_views import StlFormatError

            raise StlFormatError("stub: bad mesh")
        self.render_calls += 1
        return {view: _view_svg(view) for view in views}


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """Start a real uvicorn in-process; patch only toolchain + config boundaries."""
    data_dir = str(tmp_path_factory.mktemp("stl-api-data"))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    patch = pytest.MonkeyPatch()
    for key, value in {
        "PENPLOT_DATA_DIR": data_dir,
        "PENPLOT_STL_MAX_UPLOAD_BYTES": str(STL_MAX_UPLOAD_BYTES),
        "REDIS_CLOUD_URL": "redis://127.0.0.1:1/0",
        "DISABLE_RATELIMIT": "True",
        "PENPIXEL_HMAC_SECRET": TEST_HMAC_SECRET,
        "LOG_LEVEL": "WARNING",
    }.items():
        patch.setenv(key, value)

    router = importlib.import_module("backend.penplot.router")
    compose = importlib.import_module("backend.penplot.compose_sheet")
    app_module = importlib.import_module("backend.main")
    from backend.penplot.store import ImageStore

    settings = Settings()
    store = ImageStore(
        images_dir=settings.images_dir,
        results_dir=settings.results_dir,
        ttl_hours=settings.image_ttl_hours,
        retained_ttl_hours=settings.retained_ttl_hours,
        stl_result_ttl_hours=settings.stl_result_ttl_hours,
    )
    boundary = _Boundary()
    patch.setattr(router, "_settings", settings, raising=False)
    patch.setattr(router, "store", store, raising=False)
    patch.setattr(router, "_load_stl_bytes", boundary.load, raising=False)
    patch.setattr(router, "_render_stl_views", boundary.render, raising=False)
    # compose_sheet imports the store handle at module import time.
    patch.setattr(compose, "store", store, raising=False)

    config = uvicorn.Config(
        app_module.app, host="127.0.0.1", port=port,
        log_level="warning", lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        if not thread.is_alive():
            break
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        patch.undo()
        raise RuntimeError("STL API uvicorn thread never became healthy")

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        yield SimpleNamespace(
            client=client, base_url=base_url, store=store, settings=settings,
            router=router, compose=compose, boundary=boundary,
        )
    server.should_exit = True
    thread.join(timeout=10)
    patch.undo()


@pytest.fixture(autouse=True)
def _reset_boundary(api):
    api.boundary.reset()
    yield
    api.boundary.reset()


def _stl_bytes(tag: str = "part") -> bytes:
    return f"solid {tag}\nendsolid {tag}\n".encode()


def _import(api, data: bytes | None = None, filename: str = "part.stl"):
    return api.client.post(
        "/v1/stl/import",
        files={"file": (filename, data if data is not None else _stl_bytes(), "application/octet-stream")},
    )


def _import_id(api, data: bytes | None = None) -> str:
    resp = _import(api, data)
    assert resp.status_code == 200, resp.text
    return resp.json()["image_id"]


def _views(api, image_id: str, params: dict | None = None):
    return api.client.post("/v1/stl/views", json={"image_id": image_id, "params": params or {}})


def _three_views(api, data: bytes | None = None) -> tuple[str, dict]:
    image_id = _import_id(api, data)
    resp = _views(api, image_id)
    assert resp.status_code == 200, resp.text
    return image_id, resp.json()


def _filename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


# -- import --------------------------------------------------------------------

def test_import_returns_stored_content_addressed_mesh(api):
    data = _stl_bytes("knipex")
    resp = _import(api, data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["image_id"] == hashlib.sha256(data).hexdigest()
    assert body["format"] == "stl" and body["filename"] == "part.stl"
    assert body["faces"] == 12
    assert body["extents_mm"] == [4.0, 2.0, 1.0]
    assert body["canonical_axes"] == [2, 1, 0]
    assert body["warnings"] == [] and body["expires_at"] > "2026-01-01"
    assert os.path.exists(api.store.find_image(body["image_id"]))


@pytest.mark.parametrize("filename", ["part.obj", "part.STL.txt", "part"])
def test_import_wrong_extension_is_415(api, filename):
    resp = _import(api, filename=filename)
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE


def test_import_upload_cap_is_10_mib_end_to_end(api):
    accepted = _import(api, b"s" * STL_MAX_UPLOAD_BYTES)
    assert accepted.status_code == 200, accepted.text
    rejected = _import(api, b"s" * (STL_MAX_UPLOAD_BYTES + 1))
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE


def test_import_cap_honours_env_override(api, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv("PENPLOT_STL_MAX_UPLOAD_BYTES", "64")
        patch.setattr(api.router, "_settings", Settings(), raising=False)
        assert _import(api, b"a" * 64).status_code == 200
        assert _import(api, b"a" * 65).status_code == 413


@pytest.mark.parametrize(
    ("faces", "status", "code"),
    [
        (STL_MAX_FACES, 200, None),
        (STL_MAX_FACES + 1, 422, ErrorCode.STL_TOO_HEAVY),
    ],
)
def test_import_face_cap_boundary(api, faces, status, code):
    api.boundary.faces = faces
    resp = _import(api, _stl_bytes(f"faces-{faces}"))
    assert resp.status_code == status, resp.text
    if code:
        assert resp.json()["error"]["code"] == code


def test_import_bad_mesh_and_missing_toolchain_envelopes(api):
    api.boundary.mode = "bad"
    bad = _import(api, _stl_bytes("bad"))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == ErrorCode.STL_INVALID

    api.boundary.mode = "missing"
    missing = _import(api, _stl_bytes("missing"))
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == ErrorCode.STL_TOOLCHAIN_UNAVAILABLE
    assert "ModuleNotFoundError" not in missing.text
    assert "Traceback" not in missing.text


# -- views ---------------------------------------------------------------------

def test_views_canonical_order_stats_sidecars_and_cache(api):
    image_id = _import_id(api)
    resp = _views(api, image_id, {"views": ["end", "broad", "edge"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["image_id"] == image_id
    assert body["generator_version"] == GENERATOR_VERSION
    assert [item["view"] for item in body["views"]] == ["broad", "edge", "end"]
    assert len({item["result_id"] for item in body["views"]}) == 3
    assert api.boundary.render_calls == 1
    for item in body["views"]:
        stats = item["stats"]
        assert stats["strokes"] > 0 and stats["pen_down_mm"] > 0
        assert stats["estimated_time_s"] > 0
        svg = api.client.get(item["svg_url"])
        assert svg.status_code == 200 and "<image" not in svg.text and "<text" not in svg.text
        meta = api.client.get(item["meta_url"])
        assert meta.status_code == 200 and meta.json()["stats"] == stats
        sidecar = api.store.get_result_meta(_filename(item["svg_url"]))
        assert sidecar["view"] == item["view"]
        assert sidecar["generator_version"] == GENERATOR_VERSION
        assert sidecar["image_id"] == image_id and sidecar["params_hash"]

    again = _views(api, image_id, {"views": ["end", "broad", "edge"]})
    assert again.status_code == 200
    assert again.json() == body
    assert api.boundary.render_calls == 1  # cache hit: no geometry work


@pytest.mark.parametrize(
    "params",
    [
        {"views": []},
        {"views": ["broad", "broad"]},
        {"views": ["side"]},
        {"views": ["broad"], "slices": 1},
        {"views": ["broad"], "slices": 17},
        {"views": ["broad"], "fit_mm": 19.9},
        {"views": ["broad"], "fit_mm": 400.1},
        {"views": ["broad"], "margin_mm": -0.1},
        {"views": ["broad"], "margin_mm": 20.1},
        {"views": ["broad"], "stroke_mm": 0.09},
        {"views": ["broad"], "stroke_mm": 1.01},
        {"views": ["broad"], "extra": 1},
        {"stats": {"strokes": 1}, "price": 1},
    ],
)
def test_views_rejects_invalid_params(api, params):
    image_id = _import_id(api, _stl_bytes(f"invalid-{hashlib.sha256(str(params).encode()).hexdigest()[:8]}"))
    body = {"image_id": image_id, "params": params} if "stats" not in params else {
        "image_id": image_id, "params": {"views": ["broad"]},
        "stats": params["stats"], "price": params["price"],
    }
    resp = api.client.post("/v1/stl/views", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("slices", 2), ("slices", 16), ("fit_mm", 20.0), ("fit_mm", 400.0),
        ("margin_mm", 0.0), ("margin_mm", 20.0),
        ("stroke_mm", 0.1), ("stroke_mm", 1.0),
    ],
)
def test_views_accepts_cap_boundaries(api, key, value):
    image_id = _import_id(api, _stl_bytes(f"edge-{key}-{value}"))
    resp = _views(api, image_id, {"views": ["broad"], key: value})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["views"]) == 1


def test_views_rejects_unknown_and_non_stl_images(api):
    unknown = _views(api, "0" * 64)
    assert unknown.status_code == 404
    png = api.client.post(
        "/v1/images", files={"file": ("pic.png", png_bytes(), "image/png")}
    )
    assert png.status_code == 200
    wrong = _views(api, png.json()["image_id"])
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == ErrorCode.STL_INVALID


def test_views_missing_toolchain_envelope(api):
    image_id = _import_id(api, _stl_bytes("missing-views"))
    api.boundary.mode = "missing"
    resp = _views(api, image_id)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == ErrorCode.STL_TOOLCHAIN_UNAVAILABLE
    assert "ModuleNotFoundError" not in resp.text and "Traceback" not in resp.text


# -- compose-sheet extension ---------------------------------------------------

def _artworks(api, views: dict, names: tuple[str, ...]) -> list[dict]:
    return [
        {
            "svg": api.client.get(next(v["svg_url"] for v in views["views"] if v["view"] == name)).text,
            "result_id": next(v["result_id"] for v in views["views"] if v["view"] == name),
            "view": name,
        }
        for name in names
    ]


def _box(svg: str) -> str:
    return ET.fromstring(svg).attrib["data-kp-art-box"]


@pytest.mark.parametrize("names", [("broad", "edge"), ("broad", "edge", "end")])
def test_compose_multi_panel_preview_and_inline_parity(api, names):
    _image_id, views = _three_views(api, _stl_bytes(f"compose-{'-'.join(names)}"))
    artworks = _artworks(api, views, names)
    body = {"artworks": artworks, "lines": ["STL model"]}
    image = api.client.post("/v1/compose-sheet", json={**body, "artwork": "image"})
    inline = api.client.post("/v1/compose-sheet", json={**body, "artwork": "inline"})
    assert image.status_code == 200 and inline.status_code == 200
    assert _box(image.text) == _box(inline.text)  # one _plan_sheet, byte-identical placement
    assert ET.fromstring(image.text).attrib["viewBox"] == ET.fromstring(inline.text).attrib["viewBox"]
    image_href = ET.fromstring(image.text).find(
        './/{http://www.w3.org/2000/svg}image'
    ).attrib["href"]
    embedded = base64.b64decode(image_href.split(",", 1)[1]).decode("utf-8")
    assert embedded.count('data-kp-stl-panel="') == len(names)
    assert inline.text.count('data-kp-stl-panel="') == len(names)
    assert "<image" in image.text and "<image" not in inline.text
    assert "<text" not in inline.text
    repeat = api.client.post("/v1/compose-sheet", json={**body, "artwork": "inline"})
    assert repeat.content == inline.content  # deterministic


def test_compose_one_artwork_preserves_legacy_bytes(api):
    image_id, views = _three_views(api, _stl_bytes("one-artwork"))
    artwork = _artworks(api, views, ("broad",))[0]
    via_artworks = api.client.post(
        "/v1/compose-sheet", json={"artworks": [artwork]}
    )
    via_svg = api.client.post("/v1/compose-sheet", json={"svg": artwork["svg"]})
    assert via_artworks.status_code == via_svg.status_code == 200
    assert via_artworks.content == via_svg.content
    assert "data-kp-art-box" not in via_artworks.text  # no new marker on legacy geometry
    assert api.store.find_image(image_id) is not None


def test_compose_source_validation(api):
    _image_id, views = _three_views(api, _stl_bytes("source-validation"))
    art = _artworks(api, views, ("broad",))[0]
    cases = [
        {"svg": art["svg"], "artworks": [art]},
        {"artworks": []},
        {"artworks": [art] * 4},
        {"svg": art["svg"], "store": True},
        {"artworks": [art], "artwork": "inline", "store": True, "preview": "png"},
        {"artworks": [{**art, "result_id": "A" * 64}]},
        {"artworks": [{**art, "svg": "<svg/>"}]},
        {"artworks": [{**art, "price": 10}]},
        {"artworks": [art], "extra": 1},
    ]
    for body in cases:
        resp = api.client.post("/v1/compose-sheet", json=body)
        assert resp.status_code == 422, (body, resp.text)


def test_compose_store_writes_orderable_sheet_with_summed_stats(api):
    image_id, views = _three_views(api, _stl_bytes("stored-sheet"))
    artworks = _artworks(api, views, ("broad", "edge", "end"))
    resp = api.client.post("/v1/compose-sheet", json={
        "artworks": artworks, "artwork": "inline", "store": True,
        "lines": ["Knipex XS"],
    })
    assert resp.status_code == 200, resp.text
    sha = resp.headers["X-Result-Sha256"]
    assert sha == hashlib.sha256(resp.content).hexdigest()
    filename = f"{sha}_stlsheet.svg"
    assert resp.headers["X-Result-Url"] == f"{api.base_url}/v1/results/{filename}"
    assert api.client.get(f"/v1/results/{filename}").content == resp.content
    meta = api.store.get_result_meta(filename)
    expected = [api.store.get_result_meta(_filename(v["svg_url"]))["stats"] for v in views["views"]]
    assert meta["stats"]["strokes"] == sum(s["strokes"] for s in expected)
    assert meta["stats"]["pen_down_mm"] == round(sum(s["pen_down_mm"] for s in expected), 2)
    assert meta["stats"]["points"]["after"] == sum(s["points"]["after"] for s in expected)
    assert meta["image_id"] == image_id
    assert meta["views"] == ["broad", "edge", "end"]
    assert meta["generator_version"] == GENERATOR_VERSION and meta["params_hash"]
    served = api.client.get(f"/v1/results/{filename}/meta")
    assert served.status_code == 200 and served.json()["stats"] == meta["stats"]


# -- result gates and retention ------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "A" * 64 + "_broad_stlview.svg",
        "a" * 63 + "_broad_stlview.svg",
        "nothex_stlview.svg",
        "a" * 64 + "_stlview.txt",
        "a" * 64 + "_broad_stlview.svg.json",
        quote("../secret_stlview.svg", safe=""),
        "a" * 64 + "_stlsheet.svg",  # gate allows the name, but the file is absent
    ],
)
def test_hostile_or_unknown_result_names_are_404(api, filename):
    assert api.client.get(f"/v1/results/{filename}").status_code == 404
    assert api.client.get(f"/v1/results/{filename}/meta").status_code == 404


def test_generated_stlview_and_stlsheet_names_are_served(api):
    _image_id, views = _three_views(api, _stl_bytes("gates"))
    art = _artworks(api, views, ("broad",))[0]
    sheet = api.client.post("/v1/compose-sheet", json={
        "artworks": [art], "artwork": "inline", "store": True,
    })
    assert sheet.status_code == 200
    for filename in (_filename(views["views"][0]["svg_url"]),
                     f"{sheet.headers['X-Result-Sha256']}_stlsheet.svg"):
        assert api.client.get(f"/v1/results/{filename}").status_code == 200
        assert api.client.get(f"/v1/results/{filename}/meta").status_code == 200


def test_retain_promotes_derived_files_and_ttl_edges(api):
    image_id, views = _three_views(api, _stl_bytes("retained"))
    artworks = _artworks(api, views, ("broad", "edge"))
    sheet = api.client.post("/v1/compose-sheet", json={
        "artworks": artworks, "artwork": "inline", "store": True,
    })
    assert sheet.status_code == 200
    view_file = _filename(views["views"][0]["svg_url"])
    sheet_file = f"{sheet.headers['X-Result-Sha256']}_stlsheet.svg"

    assert api.client.post(f"/v1/images/{'f' * 64}/retain").status_code == 404
    first = api.client.post(f"/v1/images/{image_id}/retain")
    second = api.client.post(f"/v1/images/{image_id}/retain")
    assert first.status_code == second.status_code == 200  # idempotent
    assert second.json()["retained"] is True
    for filename in (view_file, f"{view_file}.json", sheet_file, f"{sheet_file}.json"):
        path = api.store.result_path(filename)
        assert os.path.exists(path + ".retained"), filename

    # Unrelated unordered result expires at 24 h; promoted files stay past it.
    other = _three_views(api, _stl_bytes("unordered"))[1]
    other_file = _filename(other["views"][0]["svg_url"])
    old = time.time() - 25 * 3600
    os.utime(api.store.result_path(other_file), (old, old))
    assert api.client.get(f"/v1/results/{other_file}").status_code == 404

    path = api.store.result_path(sheet_file)
    os.utime(path, (old, old))
    assert api.client.get(f"/v1/results/{sheet_file}").status_code == 200
    very_old = time.time() - 91 * 24 * 3600
    os.utime(path, (very_old, very_old))
    assert api.client.get(f"/v1/results/{sheet_file}").status_code == 404
