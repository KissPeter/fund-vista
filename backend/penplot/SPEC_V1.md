# Kép → Pen Plot konverter — API és architektúra terv (v1)

> This file is the frozen v1 reference. Code comments citing `spec §X`
> (e.g. `spec §2.3`, `spec §3.3`, `spec §4.1`, `spec §5`, `spec §7`) point
> to the sections below. **Part B** (after the spec) is the implementation
> and divergence log — including answers to the v1 review comments.

---

## PART A — Eredeti specifikáció (frozen)

Cél: tetszőleges kép (raszter vagy SVG) feltöltése után vonalas, pen plotterrel rajzolható SVG előállítása, paraméterezhető beállításokkal, úgy hogy egy paraméter-módosítás után **ne kelljen újra feltölteni a képet**. Minden végpont `/v1/` alatt verziózva, hogy a séma később (v2) szabadon bővíthető legyen a régi kliensek törése nélkül.

---

## 1. Architektúra áttekintés

```
Böngésző (kliens)                       Szerver (v1 API)
──────────────────                       ─────────────────
kép kiválasztás
  → Canvas resize
  → SHA-256 (Web Crypto)
  → DPI/A4 előellenőrzés
        │
        ▼
POST /v1/images ───────────────────►  kép tárolása cache-ben (image_id = sha256)
        │  ◄─────────────────────────  { image_id, width, height, warnings }
        ▼
csúszkák állítása
        │
        ▼
POST /v1/convert { image_id, params } ► feldolgozási lánc (4. fejezet)
        │  ◄─────────────────────────  { svg_url, stats, warnings }
        ▼
paraméter módosítás → csak POST /v1/convert ismét, image_id-vel, kép nélkül
```

Az `image_id` (SHA-256) a kulcs, ami elválasztja a "kép feltöltés" (ritka, drága: hálózati transzfer) és a "generálás" (gyakori, olcsó: csak paraméterek) műveleteket.

---

## 2. API végpontok (v1)

### 2.1 `POST /v1/images`

Kép feltöltése/regisztrálása. Csak akkor hívódik, ha új képet választ a felhasználó, vagy ha egy korábbi `image_id` már lejárt a cache-ből (lásd 5. fejezet).

**Kérés:** `multipart/form-data`
- `file`: a kép bájtjai (a kliens által már kicsinyített változat, lásd 6. fejezet)

**Válasz `200 OK`:**
```json
{
  "image_id": "a3f9e1c0...b21f",
  "format": "png",
  "width": 1800,
  "height": 1200,
  "is_vector": false,
  "warnings": ["low_resolution_for_a4"],
  "expires_at": "2026-09-13T10:00:00Z"
}
```

**Hibák:**
- `413 Payload Too Large` — a fájl a méretlimit felett (pl. 10 MB)
- `422 Unsupported Media Type` — nem támogatott formátum
- `400 Bad Request` — sérült/olvashatatlan fájl

Megjegyzés: a szerver **mindig újraszámolja** a SHA-256-ot a beérkezett bájtokból — a kliens soha nem küld hasht ezen a végponton, csak a fájlt. A visszakapott `image_id` a szerver számítása.

### 2.2 `GET /v1/images/{image_id}`

Opcionális, gyors ellenőrzésre: megvan-e még a kép a cache-ben (pl. ha a kliens visszatér egy korábbi munkamenethez).

**Válasz `200`:** ugyanaz a payload, mint a feltöltésnél.
**Válasz `404`:** `{ "error": "image_not_found" }` — a kliens ekkor újra feltölti a képet.

### 2.3 `POST /v1/convert`

A tényleges generálás. Ez fut le minden csúszka-módosításnál.

**Kérés:**
```json
{
  "image_id": "a3f9e1c0...b21f",
  "params": {
    "method": "hatch",
    "threshold": 128,
    "blur_radius": 1.0,
    "hatch_pitch_mm": 1.2,
    "contour_simplify": 2,
    "linemerge_tolerance_mm": 0.5,
    "linesimplify_tolerance_mm": 0.1,
    "linesort": true,
    "reloop_tolerance_mm": 0.05,
    "page": { "size": "A4", "orientation": "portrait", "margin_mm": 10 },
    "pen": { "draw_speed_mm_s": 40, "travel_speed_mm_s": 100, "pen_lift_s": 0.3 }
  }
}
```

`method`: `"contour"` | `"hatch"` | `"flow"` — melyik vonalasító algoritmus fusson (lásd 3.2).

**Válasz `200 OK`:**
```json
{
  "image_id": "a3f9e1c0...b21f",
  "svg_url": "https://.../a3f9e1c0...b21f_optimized.svg",
  "vpype_command": "read --quantization 0.02mm \"in.svg\" linemerge --tolerance 0.5mm linesimplify --tolerance 0.1mm linesort reloop --tolerance 0.05mm write --color-mode none \"out.svg\"",
  "stats": {
    "points": { "before": 1240, "after": 860 },
    "segments": { "before": 620, "after": 430 },
    "strokes": 58,
    "pen_down_mm": 4820.4,
    "pen_up_mm": 312.8,
    "estimated_time_s": 342
  },
  "warnings": []
}
```

**Hibák:**
- `404 image_not_found` — a hivatkozott `image_id` már nincs a cache-ben → kliens újra feltölti a képet, majd megismétli a hívást
- `422 invalid_params` — pl. `hatch_pitch_mm` negatív, vagy `page.size` ismeretlen
- `500 processing_failed` — a feldolgozási lánc valamelyik lépése elszállt (naplózva, a válasz nem tartalmaz technikai részletet a felhasználónak)

Nagyobb képeknél / lassabb módszereknél (pl. `flow`) érdemes lehet ezt aszinkronra váltani: `202 Accepted` + `{ "job_id": "..." }`, majd `GET /v1/jobs/{job_id}` pollozással vagy Server-Sent Events-szel jelezni az állapotot (`queued` → `processing` → `done`/`failed`). v1-ben ez opcionális bővítés, a szinkron válasz kis/közepes képeken elegendő.

---

## 3. Belső feldolgozási lánc — pontosan mi történik és milyen library végzi

A `/v1/convert` hívás mögött a következő lépések futnak le, sorban:

### 3.1 Kép betöltés és előfeldolgozás

- **Pillow** (HPND / MIT-CMU licenc, ingyenes, kereskedelmi célra is szabadon használható) — a kép betöltése, formátum-normalizálás (JPG/PNG/WEBP/BMP/TIFF stb. egységes RGB/szürkeárnyalatos tömbbé alakítása), szürkeárnyalatra konvertálás, resize, kontraszt-állítás.
- **NumPy** (BSD-3-Clause) — a pixeltömbök hatékony numerikus kezelése a további lépésekhez.
- **OpenCV** (`opencv-python`, Apache-2.0 licenc a 4.5-ös verziótól kezdve) — Gauss-elmosás (`blur_radius` paraméter), adaptív küszöbölés, Canny él­detektálás, kontúrkeresés (`findContours`) a `contour` módszerhez.

Ha a bemenet SVG, ez a lépés kimarad — a vektoros fájl közvetlenül a vpype-lánchoz kerül (3.3).

### 3.2 Vonalassá alakítás (a `method` paraméter szerint)

Ez a lépés állítja elő a nyers, még nem optimalizált vonalas SVG-t.

**`contour` mód — kontúrkövetés:**
A küszöbölt fekete-fehér bitmapet a **Potrace** algoritmus veszi ("Peter Selinger", GPL-2.0-or-later licenc — figyelem, ez copyleft, lásd 7. fejezet) alakítja sima, folytonos körvonalakká. Alternatívaként a **linedraw** (LingDong-, MIT licenc) is használható: Sobel-szűrős éldetektálás + a szomszédos élpixelek összefűzése poligonvonalakká, natívan SVG-t ír ki.

**`hatch` mód — tónusos vonalkázás:**
A **hatched** csomag (plottertools/hatched, MIT licenc) végzi: a képet tónusértékek szerint vonalkázza (`hatch_pitch_mm` = a vonalkázás sűrűsége), belül OpenCV-t (élek/kontrasztok), scikit-image-et (BSD-3-Clause, interpolációhoz) és Shapely-t (BSD-3-Clause — de a mögötte futó GEOS geometriai motor LGPL-2.1, lásd 7. fejezet) használ a vonalgeometriák létrehozásához, az eredményt pedig svgwrite-tal (MIT licenc) írja ki SVG-be.

**`flow` mód — organikus áramlásvonalak:**
A **vpype-flow-imager** plugin (GPL-3.0 licenc) áramlási mezőt (flow field) illeszt a kép tónusaira, és a sűrűségnek megfelelő görbült vonalakat generál — vizuálisan a legművészibb hatású, de a legkevésbé "kontrollált" mód.

### 3.3 vpype cleanup és plotter-optimalizálás

A 3.2-ben kapott nyers SVG-t a **vpype** (abey79/vpype, MIT licenc) CLI/Python API dolgozza fel, ugyanazzal a lánccal, amit a Drawscape SVG Optimizer is használ:

```
read --quantization <mm> in.svg
linemerge --tolerance <linemerge_tolerance_mm>
linesimplify --tolerance <linesimplify_tolerance_mm>
linesort                                            (ha params.linesort == true)
reloop --tolerance <reloop_tolerance_mm>
layout <page.size> --margin <page.margin_mm>
write --color-mode none out.svg
```

Lépésenként:
- `linemerge` — az egymáshoz közeli végpontú szakaszokat egy folytonos vonallá fűzi össze (kevesebb toll fel/le mozgás).
- `linesimplify` — a pontsűrűséget csökkenti a megadott tolerancián belül (kisebb fájl, gyorsabb rajzolás, vizuálisan nem érzékelhető veszteséggel).
- `linesort` — a vonalak sorrendjét úgy rendezi át, hogy a toll-fent utazási távolság minimális legyen (heurisztikus, két-opt jellegű optimalizálás).
- `reloop` — a zárt vonalak kezdőpontját úgy tolja el, hogy a rajzolási/emelési varratok kevésbé látszódjanak.
- `layout` — a végeredményt a megadott laptípusra (A4 stb.) és margóra igazítja.

### 3.4 Mérés és statisztika számítás

A végleges SVG minden `path`-jára:
- **pen-down hossz**: a path-ok tényleges (rajzolt) ívhosszának összege. Mivel a szerver már a vpype/Shapely geometriai objektumokból dolgozik, ez egyszerűen az egyes `LineString`/`Path` objektumok `.length` értékének összegzése — nem kell külön JS könyvtár hozzá szerver oldalon.
- **pen-up hossz**: a `linesort` utáni sorrendben egymást követő path-ok végpontja és a következő path kezdőpontja közti euklideszi távolságok összege.
- **becsült idő**: `pen_down_mm / draw_speed_mm_s + pen_up_mm / travel_speed_mm_s + strokes * pen_lift_s` (egyszerűsített modell; pontosabb, gyorsulási rámpát is figyelembe vevő becsléshez az Evil Mad Scientist AxiDraw API-jának nyílt forráskódú `estimate_time.py` mintája vehető alapul referenciaként).
- **pont-/szegmensszám előtte-utána**: a `linesimplify` előtti és utáni pontszám egyszerű összehasonlítása — ez adja a `stats.points` és `stats.segments` mezőket.

---

## 4. Kliens oldali logika

### 4.1 Kép kiválasztás után, feltöltés előtt

- Canvas API-val resize egy ésszerű max méretre (pl. hosszabb oldal ~2500–3000 px — a hatch/kontúr algoritmusoknak ez bőven elég, a natív felbontás küldése csak fölösleges sávszélesség és feldolgozási idő).
- **DPI/A4 előellenőrzés**, kizárólag raszter bemenetnél (SVG-nél nincs értelme, mert vektoros): `dpi = kép_szélesség_px / (210 mm / 25.4)`. Ha ez pl. 100 alatt van, azonnali, szerver-hívás nélküli figyelmeztetés jelenik meg ("kb. X DPI A4-es lapon, pixeles/lépcsős vonalak várhatók"). Ugyanez a szám a `warnings: ["low_resolution_for_a4"]` mezőben a szerver válaszában is megjelenik (mert a szerver is kiszámolja feltöltéskor), hogy a kliens állapota és a szerver állapota ne térjen el.
- **SHA-256 számítás** a Web Crypto API-val (`crypto.subtle.digest('SHA-256', ...)`) a **resize utáni** bájtokból — ez azért fontos, mert ennek a hash-nek pontosan azt a bájtsorozatot kell azonosítania, amit a szerver ténylegesen kap és eltárol. Ez a hash csak arra szolgál, hogy a kliens eldöntse, kell-e egyáltalán feltöltés (ha ugyanazt a képet választja újra a felhasználó, és a kliens még emlékszik a hozzá tartozó `image_id`-ra, nem hívja újra a `/v1/images` végpontot).

### 4.2 Csúszka mozgatásnál

~300–500 ms debounce, utána csak `POST /v1/convert { image_id, params }` — nincs újra feltöltés.

### 4.3 Cache-lejárat kezelése

**Ha a szerver `404 image_not_found`-ot ad** (mert lejárt a cache-ből): a kliens csendben újra elküldi a képet a `/v1/images` végpontra, megkapja az új `image_id`-t, majd megismétli a `/v1/convert` hívást ugyanazokkal a paraméterekkel — a felhasználónak ebből ideális esetben semmit nem kell észrevennie.

---

## 5. Adatmodell / cache / TTL

- Tárolás: `images/{sha256}.{ext}` egy objektumtárolóban (pl. S3-kompatibilis storage).
- TTL: pl. 24–48 óra — ez csak munkamenet-szintű cache, nem végleges adattár, a lejárt bejegyzések törlődnek.
- A kliens hash-ét a szerver **soha nem fogadja el hitelesítésként vagy azonosítóként közvetlenül** — a `/v1/images` végpont mindig a ténylegesen beérkezett bájtokból számol SHA-256-ot, a kliens csak a saját döntéséhez (kell-e újra feltölteni) használja a saját maga által számolt hash-t.
- Rate limit és fájlméret-limit (pl. 10 MB) a `/v1/images` végponton.

---

## 6. Verziózás elve

Minden végpont `/v1/...` alatt fut. Ha egy jövőbeli módosítás nem visszafelé kompatibilis (pl. a `params` séma gyökeresen változik, vagy a válasz struktúrája bővül kötelező mezőkkel), az `/v2/` alá kerül, a `/v1/` változatlanul tovább üzemel a régi kliensek számára, amíg le nem futnak a kivezetésen.

---

## 7. Felhasznált nyílt forráskódú könyvtárak

| Könyvtár | Szerep | Licenc |
|---|---|---|
| Pillow | kép betöltés, formátum-normalizálás, resize, szürkeárnyalat | HPND / MIT-CMU (permisszív) |
| NumPy | numerikus pixeltömb-műveletek | BSD-3-Clause |
| OpenCV (`opencv-python`, ≥4.5) | elmosás, küszöbölés, éldetektálás, kontúrkeresés | Apache-2.0 |
| Potrace | bitmap → sima vektor kontúr (`contour` mód) | **GPL-2.0-or-later** ⚠️ |
| linedraw | Sobel-él + vonalkövetés, natív SVG export (`contour` mód alternatívája) | MIT |
| hatched | tónusos vonalkázás (`hatch` mód) | MIT |
| scikit-image | interpoláció a `hatched` csomagon belül | BSD-3-Clause |
| Shapely | vonalgeometria-műveletek | BSD-3-Clause (a mögöttes GEOS motor **LGPL-2.1**) |
| svgwrite | SVG fájl írása | MIT |
| vpype-flow-imager | áramlásvonalas vonalrajz (`flow` mód) | **GPL-3.0** ⚠️ |
| vpype | linemerge/linesimplify/linesort/reloop/layout cleanup lánc | MIT |
| FastAPI | backend API keretrendszer | MIT |
| Pydantic | kérés/válasz séma-validáció | MIT |
| Web Crypto API | SHA-256 számítás kliens oldalon | böngésző natív API, nem külső library |

**⚠️ Licenc-figyelmeztetés:** a Potrace (GPL-2.0-or-later) és a vpype-flow-imager (GPL-3.0) copyleft licencek — ha a szolgáltatást saját, zárt forráskódú backend mögé teszed, ez önmagában nem probléma (a GPL a *terjesztett szoftverre* vonatkozik, egy webes API mögötti belső futtatás jellemzően nem minősül terjesztésnek), de ha a backend forráskódját vagy a belőle épített binárist harmadik félnek átadod/terjeszted, azzal együtt a GPL-kötelezettségek (forráskód elérhetővé tétele, licenc továbbadása) is átszállnak. Ha ezt el szeretnéd kerülni, a `contour` módhoz a Potrace helyett a linedraw (MIT) vagy ImageTracer.js (Unlicense, tisztán JS, akár kliens oldalon is futtatható) használható, a `flow` mód pedig egyszerűen elhagyható vagy házon belül újraimplementálható MIT/BSD alapokon. Ez nem jogi tanács — komolyabb terjesztési forgatókönyv esetén érdemes jogászt bevonni.

---

## PART B — Implementation & divergence log (v1, as built)

Implementation lives in `backend/penplot/`. Module map:

| File | Owns |
|---|---|
| `schemas.py` | Wire format (Pydantic is the single validation gate) |
| `errors.py` | `{"error": {"code", "message"}}` envelope |
| `config.py` | Limits/TTL/dirs (env-overridable), page table, `quantization_mm = 0.02` |
| `store.py` | Filesystem store: `images/{sha256}.{ext}`, `results/{sha256}_{paramhash}_optimized.svg`, lazy TTL |
| `imaging.py` | Pixels (Pillow/NumPy/OpenCV) + SVG `read` |
| `methods.py` | `contour`/`hatch`/`flow` behind the `LineMethod` protocol |
| `optimize.py` | layout → quantize → linemerge → linesimplify → linesort → reloop → SVG |
| `pipeline.py` | Orchestration with per-stage timing logs |
| `router.py` | Thin HTTP glue for `/v1/*` (+ `GET /v1/results/{file}` serving `svg_url`) |

Deliberate, spec-acknowledged substitution (§3.2 alternatives, §7 licence warning):
Potrace, hatched/scikit-image/Shapely, svgwrite, vpype(-flow-imager) are **not**
dependencies — contour tracing uses OpenCV `findContours`, hatching and flow
are in-house, cleanup is pure Python. The backend stays permissively licensed
(Pillow HPND / NumPy BSD / OpenCV Apache-2.0). `store.py` keeps an S3-shaped
seam for later; convert stays synchronous (the spec's optional async jobs were
not requested for v1).

### B.1 `vpype_command` semantics (review point 2 — resolved as documented)

The `vpype_command` response field keeps its spec §2.3 **name**, but it is an
**equivalent recipe, not a byte-reproducing command**. Running the string
through real vpype yields the same geometry class, not identical bytes. This
was verified against real **vpype 1.12.1** (scratch venv, CLI runs,
2026-09-12 — vpype is *not* a project dependency). Confirmed divergences:

1. **`linesimplify` backend**: upstream delegates to Shapely
   `simplify(preserve_topology=False)` (`vpype_cli/operations.py`); ours is a
   hand-rolled RDP that additionally preserves closed-ring closure (§B.2).
2. **`linesort` heuristic**: upstream runs greedy **plus 2-opt passes**
   ("two-op further reduced pen-up" in upstream source); ours is greedy
   nearest-neighbour with reversal only. Same objective, tie order may differ.
3. **`reloop` policy**: upstream **randomizes** the seam location (upstream
   docstring: "Randomize the seam location of closed paths"); ours rotates the
   seam to the point nearest the previous pen position, **deterministically**.
   The determinism is load-bearing: results are content-addressed
   (`{sha}_{paramhash}_optimized.svg`) and repeat converts must be
   byte-identical, which a random seam would break.
4. **SVG `read` subset**: §B.4. Anything outside it does not reach the chain.
5. **Quantization placement**: upstream quantizes at `read`; ours quantizes
   post-layout in mm — same units, same grid (0.02 mm), so tolerances behave
   identically downstream.

The field docstring (`optimize.build_vpype_command`) and the
`ConvertResponse.vpype_command` comment both state this; do not present the
string as reproducible.

### B.2 Closed-ring simplification (review point 1 — corrected, verified)

**Correction of an earlier claim.** During development a naive RDP collapsed a
closed ring to two *identical* points (zero-length) because a closed chord
makes every perpendicular distance read as 0 — our own test suite caught it
(`test_convert_all_methods_over_real_http[contour]`), and the fix (cut the
ring at the farthest vertex, simplify open, re-close) lives in
`optimize._rdp`. That bug was **ours, in our own code** — it was initially
described as "a bug vpype itself lacks". The side-by-side against real vpype
1.12.1 shows that phrasing was wrong:

- Upstream `linesimplify` is Shapely-backed, never contained our naive RDP,
  so it cannot have our exact bug. On a closed 5-vertex ring at small
  tolerance it preserves the ring correctly.
- Under aggressive tolerance upstream still degenerates, differently: at
  `2mm` it emitted a **2-point polygon**, at `20mm` the line **vanished
  entirely** (observed CLI output, repro `M0,0 L10,0 L10,5 L5,8 L0,5 Z`).
- Our chosen semantics: never emit zero-length collapse; rings stay closed
  through simplify (`_rdp`), fully-degenerate lines are dropped at quantize
  with stats kept consistent.

Status: upstream-bug claim **withdrawn**; the above is the verified record.
No code change needed — only this correction.

### B.3 SVG `transform=` gap (review point 3 — fixed, tested)

The review correctly identified this as the ship-blocker: Illustrator /
Inkscape / Figma output wraps paths in transformed groups, so ignoring
`transform=` meant silently-wrong geometry on the *common* vector case with
only "upload succeeds" tests to show for it. Both halves are now closed:

- **Handling**: full `transform=` support (translate/scale/rotate incl.
  center/skewX/skewY/matrix, composed through nested groups), viewBox +
  `preserveAspectRatio` mapping, CSS units, rounded rects, `display=none` /
  `visibility` filtering (attribute and `style=`), `<use href="#id">` with
  cycle guard, nested `<svg>`, `<text>` outlined via Pillow + contours, full
  path command set incl. arcs (`A`, spec F.6.5) and smooth curves (`S`/`T`),
  one polyline per subpath (`M` breaks, `Z` closes — no spurious connectors).
- **Geometry-asserting tests** (not just status codes),
  `backend/tests/test_penplot_svg_http.py`: transformed groups assert
  **byte-identical** output SVGs against pre-transformed reference documents;
  same technique covers viewBox scaling, hidden-element skipping, and `<use>`.
  Quantization tests parse served SVG coordinates and assert every value sits
  on the 0.02 mm grid.

### B.4 Remaining non-goals (documented, enforced, not silent)

From the code header (`imaging.py`, SVG section): embedded `<image>`
rasters, paint servers (gradients/patterns — outlines only), external
(`non-#`) references. Enforcement: `"image"` is in `_SKIP_TAGS`;
`_render_shape` reads geometry attributes only, never `fill`/`stroke`;
`<use>` resolves `#local-id` references exclusively. A vector file using
only these features still fails loudly with `400 bad_image`, never with
silently-wrong output. (Possible follow-up, not done: surface
`embedded_rasters_ignored`-style entries in `warnings`.)

### B.5 Test strategy

`backend/tests/`: **real HTTP only** — a live uvicorn subprocess on an
ephemeral port driven by httpx (`conftest.py`); no FastAPI TestClient. Covers
all three methods end-to-end incl. fetching `svg_url`, the one-upload /
N-converts flow, determinism (repeat convert → identical bytes), every error
envelope code, oversize/unsupported/corrupt uploads, vector input, DPI
warnings, and the §B.3 geometry-equivalence cases. 37 tests, ~2 s.

---

## PART C — Review 1 (2026-09-12)

Scope: backend implementation (`backend/penplot/`, wired in `backend/main.py`,
tested in `backend/tests/`) compared against the user-supplied v1 plan
(Part A above). Client-side §4 (Canvas resize, Web Crypto SHA-256, debounce,
silent re-upload) has **no frontend code in this repo**, so §4 is reviewed
only as "does the backend contract support it" — it does. Test evidence:
`backend/tests/test_penplot_http.py` + `test_penplot_svg_http.py`,
**37 passed in ~2 s** (run 2026-09-12, repo `.venv`, real uvicorn subprocess
over HTTP, no TestClient).

### Verdict

Core contract conforms: one upload → N converts by `image_id`, server-recomputed
SHA-256, all three methods end-to-end, deterministic content-addressed results,
typed validation with loud 422s, DPI warnings on both upload and convert.
No ship-blocker found. Open items below are ordered by severity; none requires
an API break (all fixable inside v1).

### C.1 Section-by-section conformance

| Spec | Status | Evidence |
|---|---|---|
| §1 one-upload / N-converts, `image_id = sha256` | ✅ conforms | `router.py:117` (`put_image_bytes` recomputes, never trusts client); `store.py:59-76` idempotent write; `test_param_change_needs_no_reupload`, `test_convert_is_deterministic_and_cached` |
| §2.1 `POST /v1/images` shape + 413/422/400 | ✅ conforms | `router.py:100-138`; size gate `router.py:102-108` (10 MB, `config.py:31-33`); `unsupported_media_type` / `bad_image` paths; `expires_at` via `store.py:94-99` (48 h default, `config.py:34-36`, spec allows 24–48 h) |
| §2.2 `GET /v1/images/{id}` + 404 | ✅ conforms (envelope shape differs, see C.2.1) | `router.py:141-166`, incl. non-hex → 404; `test_get_image_roundtrip`, `test_get_unknown_image_404_envelope` |
| §2.3 `POST /v1/convert` request/response | ✅ conforms | `schemas.py:38-60` (defaults match spec example), `router.py:169-220`, `pipeline.py:70-181`; `svg_url` dereferenceable via extra `GET /v1/results/{file}` (`router.py:223-238`, traversal-guarded, `store.py:103-106`) — addition, not violation (see C.2.9) |
| §2.3 errors 404/422/500 | ✅ conforms | `image_not_found` (`errors.py:39-44`), Pydantic → `invalid_params` (`router.py:62-73`, registered in `main.py:64-65`), `processing_failed` without internals (`pipeline.py:179-182`) |
| §2.3 optional async jobs | ✅ deliberately deferred | Sync-only, documented in Part B; flow has a 600-line CPU cap (`methods.py:178-180`). Recommend a load test before opening large-image `flow` to abuse |
| §3.1 Pillow/NumPy/OpenCV; SVG bypass | ✅ conforms | `imaging.py:1-116` preprocess; `pipeline.py:84-88` vector branch skips pixels |
| §3.2 contour/hatch/flow | ⚠️ documented substitution, 2 minor gaps | In-house impls (`methods.py`), no Potrace/hatched/flow-imager per §7 licence note. Gaps: hatch-pitch clip is silent (C.2.4); flow ignores `hatch_pitch`/`contour_simplify` (C.2.4) |
| §3.3 vpype chain incl. conditional linesort | ✅ conforms in effect, order differs (documented) | `pipeline.py:127-143` runs layout→quantize→merge→simplify→sort→reloop; `optimize.py:1-13` justifies (tolerances in final mm). `linesort` conditional (`pipeline.py:140`). `vpype_command` is an equivalent recipe, honestly labelled (`optimize.py:277-295`, §B.1) |
| §3.4 stats formulas | ✅ conforms, definition broader (docs-only) | pen-down/pen-up/estimate (`pipeline.py:146-163`) match spec; before/after counts raw→final incl. quantize/merge, not simplify-only (C.2.3) |
| §4 client logic | ➖ no frontend in repo | Backend side supports it: hash recompute, 404 re-upload flow, low-res hint on both upload and convert (`router.py:210-213`) |
| §5 store/TTL/limits | ⚠️ 1 gap (rate limit resolved, see C.4) | Filesystem `images/{sha}.{ext}` + lazy TTL ✅ (`store.py`); per-IP rate limit on POST `/v1/images` + `/v1/convert` ✅ (`ratelimit.py`, `router.py`); **results never expire** (C.2.3, open) |
| §6 `/v1/` versioning | ✅ conforms | `router.py:34` prefix; global `RequestValidationError` handler (`main.py:65`) only affects penplot in practice (proxy routes don't validate) |
| §7 permissive-only backend | ✅ conforms | Pillow/NumPy/OpenCV only; GPL kept out (`imaging.py:1-8`, `methods.py:1-14`) |

### C.2 Findings (ordered by severity)

**C.2.1 [docs-only] Error-envelope shape vs Part A text.**
Part A §2.2 shows a flat `{"error": "image_not_found"}`; the code (and all
tests) use the nested `{"error": {"code", "message"}}` form
(`errors.py:1-10`, `router.py:52-55`). The nested form is strictly better
(machine-readable `code` for the §4.3 re-upload switch + human `message`) and
already frozen by tests — do **not** change code. Fix: treat the nested
envelope as the v1 freeze and correct the §2.2 example text.

**Status: accepted as-is (docs-only, no code change).** The nested
`{"error": {"code", "message"}}` envelope is the frozen v1 wire contract and
every test asserts it. Part A §2.2 is under the frozen section, so its flat
example was deliberately left untouched; no fix scheduled — the review's
suggested text amendment would violate the Part A freeze.

**C.2.2 [medium] Rate limiting (§5) not implemented.**
`Rate limit és fájlméret-limit (pl. 10 MB)` — only the size half exists
(`config.py:31-33`, `router.py:102-108`). `/v1/images` and `/v1/convert`
(flow/hatch on 3000 px) are CPU-exposed with no throttle. Fix options:
per-IP token bucket with `429 + Retry-After`, or record "deferred, single-
instance v1" explicitly if a gateway provides it. Do not ship a public
endpoint without one of the two.

**Status: resolved (2026-09-12).** Per-IP fixed-window throttle on both POST
endpoints — see the C.4 resolution log below.

**C.2.3 [medium-low] Convert results have no TTL (§5).**
`images/` expire lazily (`store.py:52-92`); `results/` are written by
`store.py:108-116` with **no expiry or sweep** — unbounded disk growth, one
file per distinct param hash. Fix: same mtime-TTL sweep as images (or an LRU
cap), since results are deterministically regenerable
(`{sha}_{paramhash}_optimized.svg`, `pipeline.py:173`).

**Status: open.** Not addressed — `results/` still has no expiry or sweep.
Safe to add later (deterministic regeneration → an mtime-TTL or LRU cap
suffices); tracked as pending work, no behavioural risk.

**C.2.4 [low] Silent parameter reshaping in methods.**
(a) `hatch_pitch_mm` is converted px-side and clipped to `[2, 200]`
(`pipeline.py:103-115`) — on extreme page/image combos the requested pitch
silently changes. (b) `flow` ignores `hatch_pitch_mm`/`contour_simplify`
(`methods.py:165-182`, tone-only), and `contour` drops specks `< 4 px²`
(`methods.py:67`). All defensible, but (a) deserves an
`image_downscaled_for_performance`-style warning (e.g. `hatch_pitch_clamped`)
or a documented per-method "honoured params" table; (b)/(c) the latter.

**Status: open.** Not addressed — the `hatch_pitch_mm` clamp
(`pipeline.py:103-115`) still happens silently, and `flow` still ignores
`hatch_pitch_mm`/`contour_simplify`; the per-method honoured-params table was
not written. All behaviours remain as reviewed.

**C.2.5 [low] Mixed-content SVG is partially silent (§B.4 over-claim).**
`<image>` is in `_SKIP_TAGS` (`imaging.py:299-302`) and skipped without a
warning; `parse_svg_vectors` only 400s when **zero** polylines result
(`imaging.py:492-496`). A vector file with paths **plus** an embedded raster
therefore converts "successfully" while dropping content — the §B.4 "never
silently-wrong" claim holds only for the all-dropped case. Fix: the already-
proposed `embedded_rasters_ignored`-style `warnings` entry (one line in
`_walk_children` + passthrough in `pipeline.py`/`router.py`).

**Status: open.** Not addressed — mixed rasters are still skipped without a
`warnings` entry; the §B.4 claim covers only the all-dropped case. The fix
remains the already-proposed `embedded_rasters_ignored` surface.

**C.2.6 [low] Double image decode on every convert.**
`router.py:180-190` re-probes dims, then `pipeline.py:91` reloads the same
bytes. Cheap (no resize yet) and self-consistent, but wasteful; pass
`(src_w, src_h, is_vector)` down or cache the probe. No behaviour change.

**Status: open.** Not addressed — the cheap re-probe on every convert remains
as reviewed. No behaviour impact; purely a small speed/waste win when touched.

**C.2.7 [trivial] Dead normalisation + strict pattern.**
`ConvertRequest.image_id` accepts only lowercase hex (`schemas.py:59`), so
`body.image_id.lower()` (`router.py:171`) never sees uppercase — either allow
`[0-9a-fA-F]` then normalise, or document lowercase-only. Same class:
upload handlers return `JSONResponse` from a `response_model=ImageMetaResponse`
endpoint (`router.py:104,112,129` + `type: ignore`) — works (FastAPI bypasses
validation for `Response` subclasses) but the annotation lies; a
`Union[ImageMetaResponse, JSONResponse]` return type would be honest.

**Status: open.** Not addressed — lowercase-only `image_id` (`schemas.py:59`)
and the `response_model=… / JSONResponse` split on the upload handlers remain
as reviewed. Cosmetic only.

**C.2.8 [info] `GET /v1/results/{file}` is undocumented.**
Required (makes `svg_url` dereferenceable), correctly guarded
(`router.py:223-238`, `store.py:103-106`, traversal tests). Suggest listing it
in §2 as serving infrastructure, not a versioned API promise.

**Status: deferred (docs-only).** Not recorded in §2 — that section is under
the Part A freeze. Acknowledged as serving infrastructure; if v2 touches the
docs, it should be listed there, not promised as a versioned endpoint.

### C.3 What is notably good (keep)

- Real-HTTP test strategy (uvicorn subprocess + httpx, `tests/conftest.py`)
  covering the one-upload/N-converts flow, determinism/byte-identity,
  every error code, traversal, DPI, and geometry-equivalence — this is why
  review 1 finds no ship-blocker with confidence.
- `extra="forbid"` on all param models (`schemas.py`) — typo'd sliders 422
  instead of silent ignore (`test_convert_invalid_params_422_envelope[typo]`).
- Case-tolerant page names (`schemas.py:18`), DPI hint on **both** upload and
  convert (`router.py:210-213`), per-stage timing logs (`pipeline.py:63-67`),
  deterministic reloop for content-addressed caching (§B.1.3).

### C.4 Resolution log (2026-09-12)

**C.2.2 [medium] Rate limiting — resolved.** Both CPU-exposed POST endpoints
(`/v1/images`, `/v1/convert`) now pass through a per-IP fixed-window throttle
(`backend/penplot/ratelimit.py`, wired as a FastAPI route dependency in
`router.py`). The scheme is ported from the affilio project's
`CheapLensThrottlingMiddleware`: Redis `INCR` + `EXPIRE`, key
`fund-vista:penplot:ratelimit:{ip}:{epoch_window}`, client IP from
`x-forwarded-for` → socket peer. Two v1-specific hardenings over the affilio
original:

1. **Redis is optional.** `main.py` startup injects the client via
   `configure_redis()`; if Redis is down the limiter degrades to an
   in-process fixed-window counter so a public endpoint is never unthrottled
   (single-instance v1 — the filesystem store is already single-instance).
2. **Redis failures degrade once**, not per-request, so a dead Redis cannot
   add a connect timeout to every upload/convert.

Exhaustion returns `429` with the nested envelope (`rate_limited`), the
standard `Retry-After` header, and `X-Rate-Limit-Limit` /
`X-Rate-Limit-Requests-Left`. Config (env-overridable): `PENPLOT_RATE_LIMIT`
(default 100), `PENPLOT_RATE_LIMIT_WINDOW_S` (default 60),
`PENPLOT_RATE_LIMIT_WHITELIST`. Tests:
`backend/tests/test_penplot_ratelimit.py` — real-HTTP 429 + headers +
whitelist, plus a unit check of the memory fallback; `conftest.py` pins the
main session server to an effectively-infinite limit and an unreachable Redis
so the 37 existing tests stay hermetic.
