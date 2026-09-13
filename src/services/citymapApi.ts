import { getApiBaseUrl } from "./apiBaseUrl";

export const CITYMAP_LAYERS = [
  "highways",
  "roads",
  "paths",
  "rails",
  "aeroway",
  "waterway",
  "water",
  "buildings",
  "ferry",
] as const;

export type CitymapLayer = (typeof CITYMAP_LAYERS)[number];

export const CITYMAP_LAYER_LABELS: Record<CitymapLayer, string> = {
  highways: "Highways",
  roads: "Roads",
  paths: "Paths",
  rails: "Rails",
  aeroway: "Airports",
  waterway: "Rivers & streams",
  water: "Water",
  buildings: "Buildings",
  ferry: "Ferry routes",
};

export interface CitymapBBox {
  south: number;
  west: number;
  north: number;
  east: number;
}

export interface CitymapRenderOptions {
  city?: string;
  bbox?: CitymapBBox;
  layers: CitymapLayer[];
  minPathLenM?: number;
  width?: number;
}

export interface CitymapRenderResponse {
  city: string | null;
  display_name: string | null;
  bbox: CitymapBBox;
  layers: string[];
  path_counts: Record<string, number>;
  raw_counts: Record<string, number>;
  svg_url: string;
  attribution: string;
  warnings: string[];
  cache_hit: boolean;
}

export interface CitymapGeocodeResponse {
  city: string;
  display_name: string;
  bbox: CitymapBBox;
  lat: number;
  lon: number;
  cache_hit: boolean;
}

export interface CitymapGeocodeCandidate {
  display_name: string;
  bbox: CitymapBBox;
  lat: number;
  lon: number;
  category: string;
  type: string;
}

export interface CitymapGeocodeSearchResponse {
  city: string;
  candidates: CitymapGeocodeCandidate[];
  cache_hit: boolean;
}

const readError = async (resp: Response, fallback: string): Promise<Error> => {
  try {
    const body = await resp.json();
    if (body.error) return new Error(`${body.error.code}: ${body.error.message}`);
  } catch {
    /* fall through */
  }
  return new Error(`${fallback} (HTTP ${resp.status})`);
};

/**
 * Load a city map (city-roads style) from OpenStreetMap.
 *
 * Pass either a place name (`city: "Budapest"`, resolved via Nominatim) or
 * an explicit `bbox`, plus any combination of `layers` — each layer renders
 * as its own `<g id="citymap-<layer>">` group in the returned SVG.
 * The SVG artwork is chrome-free (no location caption, no credit comment);
 * credit OSM via the returned `attribution` field instead (ODbL 1.0).
 * Raw data and rendered SVGs are cached server-side in Redis.
 */
export async function loadCityMap(opts: CitymapRenderOptions): Promise<CitymapRenderResponse> {
  const resp = await fetch(getApiBaseUrl("/v1/citymap/render"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      city: opts.city,
      bbox: opts.bbox,
      layers: opts.layers,
      min_path_len_m: opts.minPathLenM ?? 10,
      width: opts.width ?? 1000,
    }),
  });
  if (!resp.ok) throw await readError(resp, "City map render failed");
  return resp.json();
}

export async function geocodeCity(city: string): Promise<CitymapGeocodeResponse> {
  const resp = await fetch(getApiBaseUrl(`/v1/citymap/geocode?city=${encodeURIComponent(city)}`));
  if (!resp.ok) throw await readError(resp, "Place lookup failed");
  return resp.json();
}

/**
 * Search place names and return up to `limit` ranked candidates, so the
 * caller can let the user pick among same-named places (e.g. Budapest, HU
 * vs Budapest, MO). Feed the chosen candidate's `bbox` to `loadCityMap`.
 */
export async function searchPlaces(city: string, limit = 5): Promise<CitymapGeocodeSearchResponse> {
  const resp = await fetch(
    getApiBaseUrl(`/v1/citymap/geocode/search?city=${encodeURIComponent(city)}&limit=${limit}`)
  );
  if (!resp.ok) throw await readError(resp, "Place search failed");
  return resp.json();
}

export async function getCitymapLayers(): Promise<{ id: string; label: string; description: string }[]> {
  const resp = await fetch(getApiBaseUrl("/v1/citymap/layers"));
  if (!resp.ok) throw await readError(resp, "Layer list failed");
  const body = await resp.json();
  return body.layers;
}
