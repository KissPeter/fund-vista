# Drawscape city-map source findings

Date: 2026-09-13
Skill: `.opencode/skills/document-findings/SKILL.md`

## Source curl (sanitized)

```bash
curl --url 'https://drawscape.io/api/map-render' \
  -H 'accept: */*' \
  -H 'content-type: application/json' \
  -H 'origin: https://drawscape.io' \
  -H 'referer: https://drawscape.io/products/city-maps?Color=blue_white&Frame=None&city=Zalaegerszeg%2C+Zala%2C+Hungary' \
  --data-raw '{"bounds":{"north":46.56353221442086,"south":46.51890013638027,"east":16.78244911091133,"west":16.74022041218126},"zoom":12,"tileset":"mapbox.mapbox-streets-v8","layers":["highways","roads","rails","water","waterway","buildings","aeroway","ferry"],"size":{"width_mm":245.31320000000002,"height_mm":376.9614},"color_scheme":"blue_white","paper_color":"#143e7f","pens":[{"key":"default","color":"white","title":"White #10"}],"preview":{"width_px":246,"height_px":378},"output_format":"webp"}'
```

Cookies/cart/shopify tokens stripped. Not needed to reproduce.

## What the payload means

- `bounds` + `zoom: 12`: crop box around Zalaegerszeg, HU (46.54N 16.76E).
- `tileset: mapbox.mapbox-streets-v8`: Drawscape fetches **Mapbox Streets v8** vector tiles server-side:
  `mapbox://mapbox.mapbox-streets-v8`
  `https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.mvt?access_token=...`
  Reference: `https://docs.mapbox.com/data/tilesets/reference/mapbox-streets-v8`
- `layers`: Drawscape abstraction over native Mapbox layers.
- `size.width_mm/height_mm`: poster print size. `preview 246x378 webp`: low-res preview; final is hi-res render.
- `color_scheme: blue_white, paper_color #143e7f, pens white`: monochrome re-render, not a Mapbox style raster.

## UI checkbox -> layer mapping

| UI label | Payload value | Native Mapbox v8 layer | OSM equivalent |
|---|---|---|---|
| Highways | `highways` | `road` class `motorway,motorway_link,trunk,trunk_link,primary` | `highway=motorway,trunk,primary` |
| Roads | `roads` | `road` class `secondary,tertiary,street,service,path...` | `highway=secondary,tertiary,residential,service,path` |
| Rails | `rails` | `road` class `major_rail,minor_rail,service_rail` | `railway=rail,light_rail,tram` |
| Water | `water` | `water` | `natural=water,lake` |
| Rivers & Streams | `waterway` | `waterway` | `waterway=river,stream,canal` |
| Buildings | `buildings` | `building` | `building=*` |
| Airports | `aeroway` | `aeroway` | `aeroway=runway,taxiway,apron,helipad` |
| Ferry Routes | `ferry` | `road` class `ferry` | `route=ferry` |

Native names are singular (`road,water,waterway,building,aeroway`); Drawscape pluralizes/filters them.

## Ultimate data source

Mapbox Streets v8 = proprietary Mapbox data + **OpenStreetMap replication feed** + Microsoft Open Maps (buildings) + Wikidata + Zenrin (Japan only).
For Hungary: effectively **OpenStreetMap**. Same geometry is available free via:

- Overpass API for bbox `46.5189,16.7402,46.5635,46.5635,16.7824`
- Geofabrik Hungary extract / OSM Planet
- OSMnx / QGIS

## Is Mapbox Streets v8 free?

No. Account + token + attribution (`© Mapbox © OpenStreetMap`) required. Pay-as-you-go with free tier (verify at `https://www.mapbox.com/pricing`, rates as of 2026-08):

- Vector Tiles API: ~200k tile req/mo free, then ~$0.25/1k
- Maps SDK Web: ~50k map loads/mo free, then ~$5.00/1k
- Maps SDK Mobile: ~25k MAUs/mo free, unlimited tiles inside

Drawscape pays Mapbox server-side and resells as posters. For a poster clone, use OSM directly (ODbL, attribute OSM contributors).
