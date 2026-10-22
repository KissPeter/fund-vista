/**
 * Yield cache + the yield load loop (issue #96).
 *
 * The logic lives here, not in the hook, so it can be unit tested without
 * a DOM: the hook only wires the React state setters in.
 *
 * Cache reads go through `readJsonCache` (#96): a truncated or hand-edited
 * localStorage entry must be a cache miss, never a throw. A throw used to
 * escape the async loader, so the loading flag was never reset and the
 * dashboard stayed stuck on "loading yields" forever.
 */
// The explicit ".ts" extension is required here: node's test runner (the
// only runner this repo has for the frontend) resolves ESM imports by their
// exact path, and tsconfig.app.json allows it (allowImportingTsExtensions).
import { readJsonCache } from "./jsonCache.ts";

/** The minimum a fund must offer for a yield lookup. */
export interface YieldFund {
  primaryKey: number;
}

/** Shape of a cached successful yield: the value plus its write time. */
export interface YieldCacheEntry {
  data: string;
  timestamp: number;
}

/** Shape of a cached failure: only the write time matters. */
export interface YieldFailCacheEntry {
  timestamp: number;
}

export const isYieldCacheEntry = (value: unknown): value is YieldCacheEntry =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as YieldCacheEntry).data === "string" &&
  typeof (value as YieldCacheEntry).timestamp === "number" &&
  Number.isFinite((value as YieldCacheEntry).timestamp);

export const isYieldFailCacheEntry = (value: unknown): value is YieldFailCacheEntry =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as YieldFailCacheEntry).timestamp === "number" &&
  Number.isFinite((value as YieldFailCacheEntry).timestamp);

/** localStorage key of the cached success entry for a fund and range. */
export const yieldCacheKey = (primaryKey: number, range: number): string =>
  `yield_${primaryKey}_${range}months`;

/** localStorage key of the cached failure entry for a fund and range. */
export const yieldFailCacheKey = (primaryKey: number, range: number): string =>
  `yield_fail_${primaryKey}_${range}months`;

/** What the loader needs from its environment (the hook passes React state). */
export interface YieldLoadDeps {
  /** Fetch one yield; a rejected promise is a cached failure, not a crash. */
  fetchYield: (primaryKey: number, range: number) => Promise<string | null>;
  /** Set/clear the "yields are loading" flag. */
  setLoading: (loading: boolean) => void;
  /** Report the progress in percent (0..100). */
  setProgress: (percent: number) => void;
  /** Hand the collected yields and the loaded range back to the caller. */
  onLoaded: (yields: Record<number, string>, range: number) => void;
  /** Clock, injectable for tests. */
  now?: () => number;
  /** Delay between two funds, injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

const TWELVE_HOURS_MS = 12 * 60 * 60 * 1000;
const THIRTY_MINUTES_MS = 30 * 60 * 1000;
const BETWEEN_FUNDS_DELAY_MS = 100;

/**
 * Load the yields of a fund batch, using the localStorage cache where it
 * is still fresh.
 *
 * The loading flag is cleared in a `finally` (#96): a rejected loader must
 * never leave the UI stuck on "loading yields".
 */
export async function loadYields(
  funds: YieldFund[],
  limit: number,
  range: number,
  deps: YieldLoadDeps,
): Promise<void> {
  const now = deps.now ? deps.now() : Date.now();
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  // ponytail: smaller batch lowers burst traffic; increase only if KH rate limit allows.
  const fundBatch = funds.slice(0, limit);
  const yieldsMap: Record<number, string> = {};

  deps.setLoading(true);
  deps.setProgress(0);

  try {
    for (let i = 0; i < fundBatch.length; i++) {
      const fund = fundBatch[i];
      const cacheKey = yieldCacheKey(fund.primaryKey, range);
      const failCacheKey = yieldFailCacheKey(fund.primaryKey, range);
      // A corrupt entry is a cache miss, not a throw (#96).
      const cached = readJsonCache(cacheKey, isYieldCacheEntry);
      const failCached = readJsonCache(failCacheKey, isYieldFailCacheEntry);

      // Skip if recently failed (shorter cache for failures)
      if (failCached) {
        const timeSinceFailure = now - failCached.timestamp;
        if (timeSinceFailure < THIRTY_MINUTES_MS) {
          continue;
        }
        localStorage.removeItem(failCacheKey);
      }

      // Use cached success if available
      if (cached) {
        const timeSinceCache = now - cached.timestamp;
        if (timeSinceCache < TWELVE_HOURS_MS) {
          yieldsMap[fund.primaryKey] = cached.data;
          continue;
        }
        localStorage.removeItem(cacheKey);
      }

      // Calculate yield for funds not in cache
      try {
        const yieldPercent = await deps.fetchYield(fund.primaryKey, range);

        if (yieldPercent && yieldPercent !== "null" && yieldPercent !== "0.00%") {
          yieldsMap[fund.primaryKey] = yieldPercent;

          // Cache successful result
          localStorage.setItem(cacheKey, JSON.stringify({
            data: yieldPercent,
            timestamp: now
          }));
        } else {
          // Cache failure for shorter time
          localStorage.setItem(failCacheKey, JSON.stringify({
            timestamp: now
          }));
        }
      } catch {
        // Cache failure for shorter time
        localStorage.setItem(failCacheKey, JSON.stringify({
          timestamp: now
        }));
      }

      // Update progress
      deps.setProgress(((i + 1) / fundBatch.length) * 100);

      // Add small delay to reduce rate limiting
      await sleep(BETWEEN_FUNDS_DELAY_MS);
    }

    deps.onLoaded(yieldsMap, range);
  } finally {
    // The loading flag must be cleared on every path (#96): a rejection
    // used to leave it stuck on true, which disabled the whole dashboard.
    deps.setLoading(false);
  }
}
