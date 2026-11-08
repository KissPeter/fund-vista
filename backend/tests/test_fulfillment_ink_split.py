"""Fulfillment ink-split (fund-vista #147, shop #1079) - two layers.

Unit (no server): the group-id → ink mapping, shared-color merging,
empty-layer dropping, byte-preserving re-stroke and determinism, on
synthetic drafts shaped exactly like the renderer output (traffic r3
``<g id="traffic-<layer>">`` layers; airport ``runways`` / ``osm-runways``
/ ``context-*`` / ``taxiway-labels`` groups).

API (in-process uvicorn, per test_traffic_api.py / test_airports_api.py):
the full fulfillment path over real HTTP - import mints ``image_id`` (=
``design_id``), the token is signed with the shared test secret, the
ink-split re-renders through the L4 cache, verifies the digest and
splits. Traffic stubs its L1 store + basemap (no network); airports runs
behind ``backend.upstream_mock``.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import tempfile
import threading
import time

os.environ.setdefault(
    "PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-fulfillment-")
)
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import httpx
import pytest
import uvicorn

from backend.fulfillment import ink_split
from backend.penplot import tokens as design_tokens
from backend.tests.helpers import TEST_HMAC_SECRET

# ---------------------------------------------------------------- drafts ----


def traffic_draft(layers: dict[str, list[str]]) -> str:
    """A traffic-shaped draft (renderer r3 output shape, traffic/render.py:146)."""
    attrs = {
        "motorways": ("Motorways", "#8a6d3b", "0.90"),
        "waterways": ("Waterways", "#5aa0c8", "0.70"),
        "water": ("Lakes", "#5aa0c8", "0.90"),
        "airfield": ("Airfield", "#000000", "1.00"),
        "departures": ("Departures", "#2c6fbb", "0.70"),
        "arrivals": ("Arrivals", "#222222", "0.70"),
        "holding": ("Holding", "#e07a00", "1.40"),
        "go_around": ("Go-around", "#d6204a", "1.40"),
    }
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'viewBox="0 0 1000 700" width="300mm" height="210mm">'
    ]
    for layer, paths in layers.items():
        label, stroke, sw = attrs[layer]
        out.append(
            f'<g id="traffic-{layer}" inkscape:groupmode="layer" inkscape:label="{label}" '
            f'fill="none" stroke="{stroke}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round">'
        )
        out += paths
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def airport_draft() -> str:
    """An airport-shaped draft (render.py group ids; Hershey taxiway labels)."""
    return "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 800" width="300mm" height="240mm">',
        '<g id="context-natural" fill="none" stroke="#000000" stroke-width="0.8"><path d="M 10 10 L 90 10"/></g>',
        '<g id="osm-buildings" fill="none" stroke="#000000" stroke-width="0.5"><path d="M 20 20 L 80 20"/></g>',
        '<g id="osm-runways" fill="none" stroke="#000000" stroke-width="1.2"><path d="M 100 100 L 500 100"/></g>',
        '<g id="runways" fill="none" stroke="#000000" stroke-width="1.5"><path d="M 100 120 L 500 120"/></g>',
        '<g id="taxiway-labels" fill="none"><text x="300" y="90" font-family="monospace" '
        'data-stroke-font="hershey" font-size="12" stroke="none" fill="#000000">A1</text></g>',
        '<g id="runway-marks" fill="none" stroke="#000000" stroke-width="0.8"><path d="M 100 130 L 500 130"/></g>',
        "</svg>",
    ]) + "\n"


FULL_TRAFFIC = traffic_draft({
    "motorways": ['<path d="M0,0 L100,50"/>'],
    "waterways": ['<path d="M0,10 L100,60"/>'],
    "water": ['<path d="M0,20 L100,70"/>'],
    "airfield": ['<path d="M0,30 L100,80"/>', '<circle cx="500" cy="50" r="4"/>'],
    "departures": ['<path d="M0,40 L100,90"/>'],
    "arrivals": ['<path d="M0,50 L100,100"/>'],
    "holding": ['<path d="M0,60 L100,110"/>'],
    "go_around": ['<path d="M0,70 L100,120"/>'],
})

PEN_ONLY = {"motorways", "waterways", "water", "airfield"}


def _inks_of(files: list[ink_split.InkFile]) -> dict[str, ink_split.InkFile]:
    return {f.ink: f for f in files}


# ------------------------------------------------- unit: airport mapping ----


def test_airport_runway_groups_take_the_runway_ink():
    def mapper(gid: str):
        return ink_split.airport_group_ink(gid, "black", "red")

    files = _inks_of(ink_split.split_by_ink(airport_draft(), mapper))
    assert sorted(files) == ["black", "red"]
    # The runway layer is the runway path groups present in the draft;
    # everything else (context, osm buildings, labels, runway labels) is Pen.
    assert sorted(files["red"].layers) == ["osm-runways", "runways"]
    assert "taxiway-labels" in files["black"].layers
    assert "runway-marks" in files["black"].layers


def test_airport_auto_runway_is_a_single_pen_file():
    def mapper(gid: str):
        return ink_split.airport_group_ink(gid, "black", None)

    files = ink_split.split_by_ink(airport_draft(), mapper)
    assert len(files) == 1
    assert files[0].ink == "black"
    assert len(files[0].layers) == 6  # every group rides the Pen


def test_airport_runway_same_as_pen_merges_into_one_file():
    def mapper(gid: str):
        return ink_split.airport_group_ink(gid, "red", "red")

    files = ink_split.split_by_ink(airport_draft(), mapper)
    assert [f.ink for f in files] == ["red"]


# ------------------------------------------------- unit: traffic mapping ----


def test_traffic_wire_layer_ids_map_to_draft_suffixes():
    def mapper(gid: str):
        return ink_split.traffic_group_ink(gid, "black", {
            "arrivals": "blue", "departures": "red", "holding": "red", "goAround": "blue",
        })

    files = _inks_of(ink_split.split_by_ink(FULL_TRAFFIC, mapper))
    # pen=black: basemap + airfield; red: departures + holding merged;
    # blue: arrivals + go_around merged → 3 files out of 8 layers.
    assert sorted(files) == ["black", "blue", "red"]
    assert sorted(files["black"].layers) == sorted(f"traffic-{s}" for s in PEN_ONLY)
    assert sorted(files["red"].layers) == ["traffic-departures", "traffic-holding"]
    assert sorted(files["blue"].layers) == ["traffic-arrivals", "traffic-go_around"]


def test_traffic_acceptance_case_three_files():
    """The #1079 acceptance: pen=black, departures=red, holding=red,
    go-around=blue → 3 files (black, red, blue); here with all layers drawn."""
    def mapper(gid: str):
        return ink_split.traffic_group_ink(gid, "black", {
            "departures": "red", "holding": "red", "goAround": "blue",
        })

    files = _inks_of(ink_split.split_by_ink(FULL_TRAFFIC, mapper))
    assert list(files) == ["black", "red", "blue"]  # source order
    assert files["red"].layers == ["traffic-departures", "traffic-holding"]


def test_traffic_without_layer_colors_is_single_pen():
    def mapper(gid: str):
        return ink_split.traffic_group_ink(gid, "black", None)

    files = ink_split.split_by_ink(FULL_TRAFFIC, mapper)
    assert len(files) == 1 and files[0].ink == "black"
    assert len(files[0].layers) == 8


def test_traffic_category_and_unknown_groups_ride_the_pen():
    def mapper(gid: str):
        return ink_split.traffic_group_ink(gid, "black", {"arrivals": "red"})

    draft = "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">',
        '<g id="traffic-wide" fill="none" stroke="#000000"><path d="M0,0 L1,1"/></g>',
        '<g id="traffic-brand-new-layer" fill="none" stroke="#000000"><path d="M0,2 L1,3"/></g>',
        '<g id="traffic-arrivals" fill="none" stroke="#000000"><path d="M0,4 L1,5"/></g>',
        '<path d="M0,6 L1,7"/>',  # non-group chrome: rides the Pen too
        "</svg>",
    ]) + "\n"
    files = _inks_of(ink_split.split_by_ink(draft, mapper))
    assert list(files) == ["black", "red"]
    assert files["black"].layers == ["traffic-wide", "traffic-brand-new-layer"]


def test_traffic_go_around_wire_id_maps_to_go_around_suffix():
    assert ink_split.TRAFFIC_WIRE_TO_SUFFIX["goAround"] == "go_around"


# --------------------------------------------- unit: weather mapping ----

WEATHER_LAYERS = ("coast", "borders", "streamlines", "isobars", "z500",
                  "isotherms", "markers", "cities")


def weather_draft(layers: tuple[str, ...] = WEATHER_LAYERS) -> str:
    """A weather-shaped draft (weather/render.py ``_assemble``): one group per
    layer, ``id`` and ``data-layer`` both the layer id, two paths each."""
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'viewBox="0 0 297 210" width="297mm" height="210mm">'
    ]
    for n, layer in enumerate(layers):
        out.append(
            f'<g id="{layer}" data-layer="{layer}" inkscape:groupmode="layer" '
            f'inkscape:label="{layer}" fill="none" stroke="#000000" stroke-width="0.25" '
            f'stroke-linejoin="round" stroke-linecap="round">'
        )
        out.append(f'<path d="M{n},0 L{n},10"/>')
        out.append(f'<path d="M0,{n} L10,{n}"/>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _weather_files(inks: dict[str, str] | None, pen: str = "black", draft: str | None = None):
    def mapper(gid: str):
        return ink_split.weather_group_ink(gid, pen, inks)

    return ink_split.split_by_ink(draft or weather_draft(), mapper)


def _path_lines(svg: str) -> list[str]:
    import re

    return re.findall(r'<path d="[^"]*"/>', svg)


def test_weather_data_layers_take_their_own_ink():
    files = _inks_of(_weather_files({
        "streamlines": "red", "isobars": "blue", "isotherms": "red", "markers": "blue",
    }))
    assert sorted(files) == ["black", "blue", "red"]
    assert sorted(files["red"].layers) == ["isotherms", "streamlines"]
    assert sorted(files["blue"].layers) == ["isobars", "markers"]


def test_weather_basemap_and_cities_always_ride_the_pen():
    # Even when the buyer names an ink for them, coast/borders/cities stay Pen.
    files = _inks_of(_weather_files({
        "coast": "red", "borders": "red", "cities": "red", "streamlines": "blue",
    }))
    assert sorted(files) == ["black", "blue"]
    # Unpicked data layers (isobars, z500, isotherms, markers) ride the Pen too.
    assert sorted(files["black"].layers) == sorted(
        ["borders", "cities", "coast", "isobars", "z500", "isotherms", "markers"])
    assert files["blue"].layers == ["streamlines"]


def test_weather_auto_resolves_to_the_pen_ink():
    files = _inks_of(_weather_files({"z500": "auto", "isobars": "red"}, pen="blue"))
    assert sorted(files) == ["blue", "red"]
    assert "z500" in files["blue"].layers
    assert files["red"].layers == ["isobars"]


def test_weather_single_ink_gives_one_file():
    files = _weather_files(None, pen="black")
    assert len(files) == 1 and files[0].ink == "black"
    assert files[0].layers == list(WEATHER_LAYERS)


def test_weather_all_layers_on_one_chosen_ink_gives_one_file():
    files = _weather_files({layer: "red" for layer in WEATHER_LAYERS if layer not in
                            {"coast", "borders", "cities"}}, pen="red")
    assert [f.ink for f in files] == ["red"]
    assert len(files[0].layers) == len(WEATHER_LAYERS)


def test_weather_layers_are_never_lost_or_duplicated():
    """The union of the pass paths equals the input paths, each exactly once."""
    draft = weather_draft()
    files = _weather_files({
        "streamlines": "red", "isobars": "blue", "z500": "green", "markers": "red",
    })
    union = sorted(line for f in files for line in _path_lines(f.svg))
    assert union == sorted(_path_lines(draft))
    layers = sorted(layer for f in files for layer in f.layers)
    assert layers == sorted(WEATHER_LAYERS)


def test_weather_passes_keep_the_sheet_viewbox_and_have_no_text_or_image():
    for f in _weather_files({"streamlines": "red"}):
        assert 'viewBox="0 0 297 210"' in f.svg
        assert "<text" not in f.svg and "<image" not in f.svg


def test_weather_unknown_layer_and_unknown_ink_are_refused_by_the_schema():
    from pydantic import ValidationError

    from backend.fulfillment.schemas import InkAssignment

    with pytest.raises(ValidationError):
        InkAssignment(pen="black", weather_layer_colors={"aurora": "red"})
    with pytest.raises(ValidationError):
        InkAssignment(pen="black", weather_layer_colors={"streamlines": "purple"})
    with pytest.raises(ValidationError):
        InkAssignment(pen="black", weather_layer_colors={"streamlines": "red"},
                      unexpected=1)  # extra="forbid"


# ---------------------------------------------- unit: split mechanics -------


def test_empty_layers_produce_nothing():
    draft = traffic_draft({
        "motorways": ['<path d="M0,0 L100,50"/>'],
        "go_around": [],  # drawn nowhere: no file may promise this ink
        "holding": ["   "],  # whitespace-only body is empty too
    })
    files = ink_split.split_by_ink(draft, lambda gid: "red")
    assert len(files) == 1
    assert files[0].ink == "red"
    assert files[0].layers == ["traffic-motorways"]
    assert "traffic-go_around" not in files[0].svg


def test_groups_merge_in_source_order_and_files_carry_root():
    files = ink_split.split_by_ink(FULL_TRAFFIC, lambda gid: "black")
    svg = files[0].svg
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert svg.rstrip().endswith("</svg>")
    # Source order preserved inside the pass: the draft's own layer order
    # (traffic/render.py ORDER: motorways, waterways, water, airfield).
    at = [svg.index(f'id="traffic-{g}"') for g in ("motorways", "waterways", "water", "airfield")]
    assert at == sorted(at)
    # The single pass carries ALL layers (everything rides the Pen here).
    assert len(files[0].layers) == 8


def test_carried_groups_are_restroked_to_the_pass_hex():
    files = ink_split.split_by_ink(FULL_TRAFFIC, lambda gid: "blue")
    body = files[0].svg
    assert 'stroke="#2456a6"' in body
    assert 'stroke="#8a6d3b"' not in body  # the old pen default is gone
    # Group attributes otherwise verbatim (inkscape labels survive).
    assert 'inkscape:label="Motorways"' in body


def test_split_is_deterministic():
    def mapper(gid: str):
        return ink_split.traffic_group_ink(gid, "black", {"departures": "red"})

    first = [f.svg for f in ink_split.split_by_ink(FULL_TRAFFIC, mapper)]
    second = [f.svg for f in ink_split.split_by_ink(FULL_TRAFFIC, mapper)]
    assert first == second


def test_design_id_is_the_draft_sha256():
    assert ink_split.design_id_for(FULL_TRAFFIC) == (
        __import__("hashlib").sha256(FULL_TRAFFIC.encode("utf-8")).hexdigest()
    )


# ------------------------------------------------------------- API ----------

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "traffic_day_lhbp.json")

HU_ROW = {
    "ident": "LHBP", "iso_country": "HU", "name": "Budapest Liszt Ferenc",
    "latitude_deg": "47.4369", "longitude_deg": "19.2556", "elevation_ft": "495",
}


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
        full_app, host="127.0.0.1", port=port, log_level="warning",
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
            raise RuntimeError("fulfillment test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """Mock edge, HMAC secret, caches - per test (shared module server)."""
    from backend import upstream_mock
    from backend.airports import cache as airports_cache
    from backend.fulfillment import router as fulfillment_router
    from backend.penplot import router as penplot_router
    from backend.traffic import cache as traffic_cache
    from backend.traffic.store import FileSliceStore
    import backend.traffic.store as traffic_store

    from backend.airports import sources

    upstream_mock.install()
    # The Overture reader is replaced by a deterministic fake (no S3, no
    # DuckDB); the outage window is cleared before and after every test.
    monkeypatch.setattr(sources, "read_overture_airfield", lambda *a: [])
    sources.reset_overture_state()
    # Settings is frozen: swap the module attribute for a copy with the
    # shared test secret (the subprocess conftest env does the same for the
    # session server).
    monkeypatch.setattr(
        fulfillment_router, "_settings",
        fulfillment_router._settings.model_copy(update={"hmac_secret": TEST_HMAC_SECRET}),
    )
    airports_cache.configure_airports_redis(None)
    penplot_router._limiter._memory.clear()
    traffic_cache.configure_traffic_redis(None)
    monkeypatch.setattr(traffic_store, "default_store",
                        lambda: FileSliceStore(tmp_path / "traffic"))
    yield
    sources.reset_overture_state()
    upstream_mock.uninstall()


def _mint(design_id: str, exp: int | None = None) -> dict:
    return {
        "design_id": design_id,
        "exp": exp or int(time.time()) + 600,
        "sig": design_tokens.sign_design_token(
            design_id=design_id, exp=exp or int(time.time()) + 600,
            secret=TEST_HMAC_SECRET,
        ),
    }


def _seed_l1(tmp_dir: str, day_iso: str) -> dict:
    """Seed the traffic L1 store with the fixture day (shifted onto it)."""
    import datetime as _dt

    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    base = _dt.datetime(2026, 10, 10, tzinfo=_dt.timezone.utc).timestamp()
    want = _dt.datetime.combine(_dt.date.fromisoformat(day_iso),
                                _dt.time(0), tzinfo=_dt.timezone.utc).timestamp()
    delta = want - base
    for fl in data["flights"]:
        fl["pts"] = [[p[0] + delta, p[1], p[2], p[3], p[4]] for p in fl["pts"]]
        fl["calls"] = [[c[0] + delta, c[1]] for c in fl["calls"]]
    data["date"] = day_iso
    from backend.traffic.store import FileSliceStore, day_key

    FileSliceStore(tmp_dir).put(
        day_key(day_iso, "LHBP"),
        gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8")))
    return data


def _stub_traffic(monkeypatch):
    """Offline traffic: airport resolution + a one-line basemap."""
    async def _fake_resolve(icao: str):
        return (HU_ROW, False)

    async def _fake_basemap(view, lat, lon):
        s, n, w, e = view
        line = [[[w, (s + n) / 2], [e, (s + n) / 2]]]
        return ({"motorways": line, "waterways": [], "water": [], "airfield": []}, [])

    monkeypatch.setattr("backend.traffic.router.resolve_airport", _fake_resolve)
    monkeypatch.setattr("backend.traffic.router.load_basemap", _fake_basemap)


def _traffic_params(day: str, **kw) -> dict:
    body = {"icao": "LHBP", "date": day}
    body.update(kw)
    return body


def test_ink_split_traffic_three_passes_over_http(live, monkeypatch, tmp_path):
    """The #1079/#147 acceptance case end to end: import → token → split."""
    import datetime as _dt

    day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date().isoformat()
    _seed_l1(str(tmp_path / "traffic"), day)
    _stub_traffic(monkeypatch)

    params = _traffic_params(day)
    imported = live.post("/v1/traffic/import", json=params)
    assert imported.status_code == 200, imported.text
    design_id = imported.json()["image_id"]

    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint(design_id),
        "kind": "traffic",
        "params": {"kind": "traffic", **params},
        "inks": {"pen": "black",
                 "layer_colors": {"departures": "red", "holding": "red", "goAround": "blue"}},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["design_id"] == design_id and body["kind"] == "traffic"
    # departures + holding share red → one file; arrivals + basemap ride the
    # Pen; the fixture day has no go-arounds → the blue layer is empty → no
    # blue file (empty layers produce nothing).
    # departures + holding share red → one file; arrivals + basemap ride the
    # Pen; the fixture day has no go-arounds → the blue layer is empty → no
    # blue file (empty layers produce nothing).
    files = {f["ink"]: f for f in body["files"]}
    assert sorted(files) == ["black", "red"]
    assert "traffic-departures" in files["red"]["layers"]
    assert "traffic-holding" in files["red"]["layers"]
    assert all(f["svg"].startswith("<svg") for f in body["files"])
    assert "adsb.lol" in body["attribution"]


def test_ink_split_airport_two_passes_over_http(live, monkeypatch):
    """Two-pen runway order (shop #1079 acceptance case 1) → 2 files."""
    from backend.upstream_mock import install  # noqa: F401  (installed by _reset)

    imported = live.post("/v1/airports/import", json={"icao": "LHBP"})
    assert imported.status_code == 200, imported.text
    design_id = imported.json()["image_id"]

    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint(design_id),
        "kind": "airport",
        "params": {"icao": "LHBP"},
        "inks": {"pen": "black", "runway": "red"},
    })
    assert resp.status_code == 200, resp.text
    files = {f["ink"]: f for f in resp.json()["files"]}
    assert sorted(files) == ["black", "red"]
    # Every runway group the mock data emitted went to the runway ink; the
    # Pen file carries everything else (context, osm buildings, labels).
    assert files["red"]["layers"]  # the mock airport has drawn runways
    assert set(files["red"]["layers"]) <= {"osm-runways", "runways"}
    assert files["black"]["layers"]
    assert not (set(files["black"]["layers"]) & {"osm-runways", "runways"})


def test_ink_split_with_convert_params_draws_the_furniture_once(live):
    """The buyer's framed preview params ride the split: the Pen pass carries
    the page frame and the title strokes, the runway pass only the artwork,
    and both are laid out on the same A4 sheet."""
    imported = live.post("/v1/airports/import", json={"icao": "LHBP"})
    assert imported.status_code == 200, imported.text
    design_id = imported.json()["image_id"]
    request = {
        "design": _mint(design_id),
        "kind": "airport",
        "params": {"icao": "LHBP"},
        "inks": {"pen": "black", "runway": "red"},
    }
    plain = live.post("/v1/fulfill/ink-split", json=request)
    framed = live.post("/v1/fulfill/ink-split", json={
        **request,
        "convert": {
            "methods": ["contour"],
            "page": {"frame": True, "margin_mm": 10},
            "label": {"enabled": True, "text": "BUDAPEST\nLHBP", "border": True},
        },
    })
    assert plain.status_code == 200 and framed.status_code == 200, framed.text
    files = {f["ink"]: f for f in framed.json()["files"]}
    assert sorted(files) == ["black", "red"]
    base = {f["ink"]: f for f in plain.json()["files"]}
    # Laid out on the A4 sheet, not the draft's own canvas.
    assert 'width="210' in files["black"]["svg"] and 'height="297' in files["black"]["svg"]
    # Furniture strokes are extra geometry only on the Pen pass.
    def strokes(svg: str) -> int:
        return svg.count("<path") + svg.count("<polyline") + svg.count("<line")
    assert strokes(files["black"]["svg"]) > 0 and strokes(files["red"]["svg"]) > 0
    again = live.post("/v1/fulfill/ink-split", json={
        **request,
        "convert": {
            "methods": ["contour"],
            "page": {"frame": False},
            "label": {"enabled": False},
        },
    })
    bare = {f["ink"]: f for f in again.json()["files"]}
    assert strokes(files["black"]["svg"]) > strokes(bare["black"]["svg"])
    assert strokes(files["red"]["svg"]) == strokes(bare["red"]["svg"])
    assert base["black"]["svg"] != files["black"]["svg"]


def test_ink_split_serves_the_cached_draft(live, monkeypatch, tmp_path):
    """Cache-first: the second call hits the L4 draft cache and splits the
    byte-identical draft (cache_hit=True, same digests)."""
    import datetime as _dt

    day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date().isoformat()
    _seed_l1(str(tmp_path / "traffic"), day)
    _stub_traffic(monkeypatch)
    params = _traffic_params(day)
    design_id = live.post("/v1/traffic/import", json=params).json()["image_id"]

    request = {
        "design": _mint(design_id), "kind": "traffic",
        "params": params, "inks": {"pen": "black"},
    }
    first = live.post("/v1/fulfill/ink-split", json=request)
    assert first.status_code == 200, first.text
    second = live.post("/v1/fulfill/ink-split", json=request)
    assert second.status_code == 200, second.text
    assert first.json()["cache_hit"] in (True, False)  # first may hit import's L4 write
    assert second.json()["cache_hit"] is True
    assert [f["svg"] for f in first.json()["files"]] == [f["svg"] for f in second.json()["files"]]


def test_ink_split_refuses_a_bad_token(live, monkeypatch, tmp_path):
    import datetime as _dt

    day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date().isoformat()
    _seed_l1(str(tmp_path / "traffic"), day)
    _stub_traffic(monkeypatch)
    params = _traffic_params(day)
    design_id = live.post("/v1/traffic/import", json=params).json()["image_id"]

    # Tampered signature → 403 before any render work.
    token = _mint(design_id)
    token["sig"] = "0" * 64
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": token, "kind": "traffic", "params": params,
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "token_invalid"

    # Expired token → 410.
    expired = _mint(design_id, exp=int(time.time()) - 10)
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": expired, "kind": "traffic", "params": params,
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "token_expired"


def test_ink_split_refuses_params_that_do_not_match_the_schema(live):
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint("a" * 64), "kind": "traffic",
        "params": {"icao": "LHBP", "date": "2026-01-01", "bogus_key": 1},
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_ink_split_refuses_unknown_layer_ids(live):
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint("a" * 64), "kind": "traffic",
        "params": {"icao": "LHBP", "date": "2026-01-01"},
        "inks": {"pen": "black", "layer_colors": {"cockpit": "red"}},
    })
    assert resp.status_code == 422


def test_ink_split_refuses_a_design_mismatch(live, monkeypatch, tmp_path):
    """A re-render that no longer hashes to the design_id fails loudly."""
    import datetime as _dt

    day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date().isoformat()
    _seed_l1(str(tmp_path / "traffic"), day)
    _stub_traffic(monkeypatch)
    params = _traffic_params(day)
    live.post("/v1/traffic/import", json=params)  # draft cached under these params

    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint("b" * 64),  # a different design's id
        "kind": "traffic", "params": params, "inks": {"pen": "black"},
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "design_mismatch"


def test_ink_split_refuses_an_unplottable_pass(live, monkeypatch):
    """A pass the import parser rejects (nothing convertible) → 422, never
    a silent unplottable file for the plotter."""
    from backend.fulfillment.ink_split import design_id_for

    unplottable = "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">',
        '<g id="traffic-motorways" fill="none" stroke="#000000">'
        '<image href="data:image/png;base64,aaaa" x="0" y="0" width="4" height="4"/></g>',
        "</svg>",
    ]) + "\n"
    doc = {"svg_text": unplottable, "warnings": [], "cache_hit": False, "icao": "LHBP"}

    async def _fake_doc(body, request, endpoint, started_mono):
        return dict(doc)

    monkeypatch.setattr("backend.traffic.router.render_document", _fake_doc)
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint(design_id_for(unplottable)),
        "kind": "traffic", "params": {"icao": "LHBP", "date": "2026-01-01"},
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unplottable_pass"


def test_green_is_an_accepted_ink_everywhere():
    """Green is the fourth ink of the shop's white-paper pen matrix (shop
    #923/#1078): a green runway, layer pick or Pen is a valid order, and the
    pass is re-stroked to the shop's green hex."""
    from backend.fulfillment.schemas import InkAssignment

    inks = InkAssignment(pen="green", runway="green")
    assert inks.runway == "green" and inks.pen == "green"
    traffic = InkAssignment(pen="black", layer_colors={"departures": "green"})
    assert traffic.layer_colors == {"departures": "green"}
    assert ink_split.INK_HEX["green"] == "#1e7a46"


def test_ink_split_of_an_all_empty_draft_serves_no_files(live, monkeypatch):
    """Every layer unticked + nothing drawn: the split is an honest empty
    list (200), not an error - there is nothing to plot."""
    from backend.fulfillment.ink_split import design_id_for

    empty_layers = "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">',
        '<g id="traffic-motorways" fill="none" stroke="#000000"></g>',
        '<g id="traffic-arrivals" fill="none" stroke="#000000"> </g>',
        "</svg>",
    ]) + "\n"

    async def _fake_doc(body, request, endpoint, started_mono):
        return {"svg_text": empty_layers, "warnings": [], "cache_hit": False, "icao": "LHBP"}

    monkeypatch.setattr("backend.traffic.router.render_document", _fake_doc)
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint(design_id_for(empty_layers)),
        "kind": "traffic", "params": {"icao": "LHBP", "date": "2026-01-01"},
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == []


# ------------------------------------------- stored design first (fallback) --
#
# The purchased design's own bytes are read from the image store before any
# re-render. These tests pin that order: a re-render that would raise must not
# run while the stored copy is intact, and every failure of the stored copy
# falls back to the unchanged re-render path.

STORED_MARK = "fulfilled_from_stored_design"


def _no_rerender(monkeypatch):
    """Any re-render is a loud failure: the stored path must never call it."""
    async def _boom(*args, **kwargs):
        raise AssertionError("render_document ran although the stored design is intact")

    monkeypatch.setattr("backend.traffic.router.render_document", _boom)
    monkeypatch.setattr("backend.airports.router.render_document", _boom)
    monkeypatch.setattr("backend.weather.service.render", _boom)


WEATHER_BODY = {"date": "2005-05-20", "time": 12, "layers": list(WEATHER_LAYERS)}


def _stub_weather(monkeypatch):
    """Offline weather: the fixture field reader and geography (no network)."""
    from backend.tests.weather_render_fixtures import FakeReader, fixture_geo
    from backend.weather import cache as weather_cache
    from backend.weather import service
    from backend.weather.fields import load_subset as real_load_subset

    reader = FakeReader()

    async def fake_load_subset(when, region, _reader, fields):
        return await real_load_subset(when, region, reader, fields)

    monkeypatch.setattr(service, "load_subset", fake_load_subset)
    monkeypatch.setattr(service, "load_geography", lambda: fixture_geo())
    weather_cache.configure_weather_redis(None)


def _order(live, monkeypatch, tmp_path, kind: str) -> tuple[str, dict]:
    """Import one design of ``kind``; return (design_id, ink-split request)."""
    import datetime as _dt

    if kind == "traffic":
        day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date().isoformat()
        _seed_l1(str(tmp_path / "traffic"), day)
        _stub_traffic(monkeypatch)
        params = _traffic_params(day)
        imported = live.post("/v1/traffic/import", json=params)
        request = {
            "kind": "traffic", "params": {"kind": "traffic", **params},
            "inks": {"pen": "black", "layer_colors": {"departures": "red"}},
        }
    elif kind == "weather":
        _stub_weather(monkeypatch)
        imported = live.post("/v1/weather/import", json=WEATHER_BODY)
        request = {
            "kind": "weather", "params": {"kind": "weather", **WEATHER_BODY},
            "inks": {"pen": "black", "weather_layer_colors": {
                "streamlines": "red", "isobars": "blue", "isotherms": "auto",
            }},
        }
    else:
        imported = live.post("/v1/airports/import", json={"icao": "LHBP"})
        request = {
            "kind": "airport", "params": {"icao": "LHBP"},
            "inks": {"pen": "black", "runway": "red"},
        }
    assert imported.status_code == 200, imported.text
    design_id = imported.json()["image_id"]
    return design_id, {"design": _mint(design_id), **request}


def _stored_path(design_id: str) -> str:
    from backend.penplot.router import store as penplot_store

    path = penplot_store.find_image(design_id)
    assert path is not None, "the import should have stored the design"
    return path


KINDS = ["traffic", "airport", "weather"]
#: Kinds whose missing or damaged stored copy may be re-rendered. Weather never
#: re-renders (ERA5T values are revised after purchase): see the weather tests.
RERENDER_KINDS = ["traffic", "airport"]


@pytest.mark.parametrize("kind", KINDS)
def test_stored_design_is_split_without_rerendering(live, monkeypatch, tmp_path, kind):
    """(a) The intact stored design is split; no renderer or upstream runs."""
    design_id, request = _order(live, monkeypatch, tmp_path, kind)
    _no_rerender(monkeypatch)

    resp = live.post("/v1/fulfill/ink-split", json=request)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["design_id"] == design_id and body["kind"] == kind
    assert body["cache_hit"] is True
    assert body["warnings"] == [STORED_MARK]
    assert body["files"], "the stored design has drawn layers"
    assert all(f["svg"].startswith("<svg") for f in body["files"])
    expected = {"traffic": "adsb.lol", "airport": "OpenStreetMap"}.get(kind, "")
    if kind == "weather":
        from backend.weather.config import ATTRIBUTION as WEATHER_ATTRIBUTION

        assert body["attribution"] == WEATHER_ATTRIBUTION
    else:
        assert expected in body["attribution"]


@pytest.mark.parametrize("kind", RERENDER_KINDS)
@pytest.mark.parametrize("corruption", ["other_svg", "not_utf8"])
def test_corrupt_stored_design_falls_back_to_rerender(
        live, monkeypatch, tmp_path, kind, corruption):
    """(b) A stored file whose bytes fail the hash check (or are not UTF-8)
    is ignored: the re-render serves the order, without the stored marker."""
    design_id, request = _order(live, monkeypatch, tmp_path, kind)
    path = _stored_path(design_id)
    with open(path, "rb") as fh:
        original = fh.read()
    corrupt = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
        if corruption == "other_svg" else b"\xff\xfe\x00 not text"
    )
    try:
        with open(path, "wb") as fh:
            fh.write(corrupt)
        resp = live.post("/v1/fulfill/ink-split", json=request)
    finally:
        # The store is content-addressed: restore the bytes for later tests.
        with open(path, "wb") as fh:
            fh.write(original)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert STORED_MARK not in body["warnings"]
    assert body["files"]


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.mark.parametrize("kind", RERENDER_KINDS)
def test_no_stored_design_rerenders_and_mismatch_is_still_409(
        live, monkeypatch, tmp_path, kind):
    """(c) With no stored copy the re-render path runs as before; a draft that
    does not hash to the design_id is still refused with 409 design_mismatch."""
    design_id, request = _order(live, monkeypatch, tmp_path, kind)
    path = _stored_path(design_id)
    original = _read(path)
    try:
        os.remove(path)
        resp = live.post("/v1/fulfill/ink-split", json=request)
        mismatch = live.post("/v1/fulfill/ink-split", json={
            **request, "design": _mint("b" * 64),
        })
    finally:
        # Shared, content-addressed store: put the purchased bytes back.
        _write(path, original)
    assert resp.status_code == 200, resp.text
    assert STORED_MARK not in resp.json()["warnings"]
    assert resp.json()["files"]
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "design_mismatch"


@pytest.mark.parametrize("kind", RERENDER_KINDS)
def test_non_svg_stored_design_falls_back(live, monkeypatch, tmp_path, kind):
    """(d) A stored copy that is not an SVG file is never used, even when its
    bytes would hash back to the design: the re-render serves the order."""
    design_id, request = _order(live, monkeypatch, tmp_path, kind)
    path = _stored_path(design_id)
    png = path[: -len(".svg")] + ".png"
    os.replace(path, png)
    try:
        resp = live.post("/v1/fulfill/ink-split", json=request)
    finally:
        os.replace(png, path)
    assert resp.status_code == 200, resp.text
    assert STORED_MARK not in resp.json()["warnings"]
    assert resp.json()["files"]


def test_stored_design_name_is_not_trusted(live, monkeypatch, tmp_path):
    """The file name only selects a candidate. Real swap: the airport design's
    file is renamed onto the traffic design's id. The digest check refuses it
    and the traffic order is re-rendered; the renamed file is not used."""
    from backend.fulfillment import router as fulfillment_router

    airport_id, _ = _order(live, monkeypatch, tmp_path, "airport")
    traffic_id, traffic_req = _order(live, monkeypatch, tmp_path, "traffic")
    a_path, b_path = _stored_path(airport_id), _stored_path(traffic_id)
    a_bytes, b_bytes = _read(a_path), _read(b_path)
    assert a_bytes != b_bytes
    try:
        os.replace(a_path, b_path)  # airport bytes now sit under the traffic id
        assert fulfillment_router._stored_design_svg(traffic_id) is None
        resp = live.post("/v1/fulfill/ink-split", json=traffic_req)
    finally:
        _write(a_path, a_bytes)
        _write(b_path, b_bytes)
    assert resp.status_code == 200, resp.text
    assert STORED_MARK not in resp.json()["warnings"]
    assert resp.json()["files"]


def test_stored_design_is_served_despite_schema_drift(live, monkeypatch, tmp_path):
    """Stored design + persisted params that no longer match the render schema
    -> still 200 from the intact stored design (no re-render, no 422)."""
    design_id, request = _order(live, monkeypatch, tmp_path, "airport")
    _no_rerender(monkeypatch)
    request["params"] = {"icao": "LHBP", "retired_field": 1}
    resp = live.post("/v1/fulfill/ink-split", json=request)
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [STORED_MARK]
    assert resp.json()["design_id"] == design_id


def test_no_stored_design_and_bad_params_is_422(live):
    """Without a stored copy the params are validated as before: 422."""
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint("a" * 64), "kind": "airport",
        "params": {"icao": "LHBP", "retired_field": 1},
        "inks": {"pen": "black"},
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_weather_ink_split_over_http(live, monkeypatch, tmp_path):
    """Import → token → split: one file per distinct ink, sha256 = design_id."""
    design_id, request = _order(live, monkeypatch, tmp_path, "weather")
    resp = live.post("/v1/fulfill/ink-split", json=request)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["design_id"] == design_id and body["kind"] == "weather"
    assert _digest_of(_stored_path(design_id)) == design_id
    inks = {f["ink"]: f for f in body["files"]}
    assert sorted(inks) == ["black", "blue", "red"]
    assert "streamlines" in inks["red"]["layers"]
    assert "isobars" in inks["blue"]["layers"]
    assert "coast" in inks["black"]["layers"]
    assert all("<text" not in f["svg"] and "<image" not in f["svg"] for f in body["files"])


def _digest_of(path: str) -> str:
    return ink_split.design_id_for(_read(path).decode("utf-8"))


def test_weather_unknown_ink_and_layer_are_422_over_http(live):
    base = {"design": _mint("a" * 64), "kind": "weather", "params": WEATHER_BODY}
    unknown_ink = live.post("/v1/fulfill/ink-split", json={
        **base, "inks": {"pen": "black", "weather_layer_colors": {"streamlines": "purple"}},
    })
    unknown_layer = live.post("/v1/fulfill/ink-split", json={
        **base, "inks": {"pen": "black", "weather_layer_colors": {"aurora": "red"}},
    })
    assert unknown_ink.status_code == 422, unknown_ink.text
    assert unknown_layer.status_code == 422, unknown_layer.text


@pytest.mark.parametrize("damage", ["missing", "other_svg"])
def test_weather_never_rerenders_without_an_intact_copy(live, monkeypatch, tmp_path, damage):
    """A paid weather order whose retained copy is gone answers 410 and never
    re-renders: a re-render hashes differently after ERA5T revisions."""
    design_id, request = _order(live, monkeypatch, tmp_path, "weather")
    _no_rerender(monkeypatch)
    path = _stored_path(design_id)
    original = _read(path)
    try:
        if damage == "missing":
            os.remove(path)
        else:
            _write(path, b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>')
        resp = live.post("/v1/fulfill/ink-split", json=request)
    finally:
        _write(path, original)
    assert resp.status_code == 410, resp.text
    assert resp.json()["error"]["code"] == "weather_design_unavailable"


def test_weather_import_is_retained_for_the_order_window(live, monkeypatch, tmp_path):
    """The weather import promotes its copy to the retained TTL (90 days)."""
    from backend.penplot.router import store as penplot_store

    design_id, _ = _order(live, monkeypatch, tmp_path, "weather")
    assert penplot_store.is_retained(_stored_path(design_id))


def test_weather_pen_only_layer_key_is_422_over_http(live):
    resp = live.post("/v1/fulfill/ink-split", json={
        "design": _mint("a" * 64), "kind": "weather", "params": WEATHER_BODY,
        "inks": {"pen": "black", "weather_layer_colors": {"coast": "red"}},
    })
    assert resp.status_code == 422, resp.text


def test_weather_pen_only_layer_key_is_refused_by_the_schema():
    from pydantic import ValidationError

    from backend.fulfillment.schemas import InkAssignment

    for layer in ("coast", "borders", "cities"):
        with pytest.raises(ValidationError):
            InkAssignment(pen="black", weather_layer_colors={layer: "red"})
    InkAssignment(pen="black", weather_layer_colors={"streamlines": "red"})  # allowed

