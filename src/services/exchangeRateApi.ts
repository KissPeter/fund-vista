const FX_BASE_URL = "https://api.frankfurter.app";
const FX_LATEST_CACHE_TTL_MS = 6 * 60 * 60 * 1000;

interface FxResponse {
  rates?: Record<string, number>;
}

const getCachedRate = (key: string, ttlMs?: number): number | null => {
  const cached = localStorage.getItem(key);
  if (!cached) return null;

  try {
    const parsed = JSON.parse(cached) as { rate: number; timestamp: number };
    if (!Number.isFinite(parsed.rate) || !Number.isFinite(parsed.timestamp)) {
      localStorage.removeItem(key);
      return null;
    }
    if (ttlMs && Date.now() - parsed.timestamp > ttlMs) {
      localStorage.removeItem(key);
      return null;
    }
    return parsed.rate;
  } catch {
    localStorage.removeItem(key);
    return null;
  }
};

const setCachedRate = (key: string, rate: number) => {
  localStorage.setItem(
    key,
    JSON.stringify({
      rate,
      timestamp: Date.now(),
    })
  );
};

export const exchangeRateApi = {
  async getRateToHuf(currency: string, date?: string): Promise<number> {
    const normalized = (currency || "HUF").toUpperCase();
    if (normalized === "HUF") return 1;

    const historicalKey = date ? `fx_huf_${normalized}_${date}` : "";
    const latestKey = `fx_huf_${normalized}_latest`;
    const cacheKey = date ? historicalKey : latestKey;
    const cached = getCachedRate(cacheKey, date ? undefined : FX_LATEST_CACHE_TTL_MS);
    if (cached !== null) return cached;

    const url = date
      ? `${FX_BASE_URL}/${date}?from=${normalized}&to=HUF`
      : `${FX_BASE_URL}/latest?from=${normalized}&to=HUF`;
    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(`Failed to fetch FX rate for ${normalized}/HUF`);
    }

    const body = (await response.json()) as FxResponse;
    const rate = body.rates?.HUF;
    if (!rate || !Number.isFinite(rate)) {
      throw new Error(`Invalid FX rate for ${normalized}/HUF`);
    }

    // ponytail: cache rates in browser; swap provider only if this API becomes unavailable.
    setCachedRate(cacheKey, rate);
    return rate;
  },
};
