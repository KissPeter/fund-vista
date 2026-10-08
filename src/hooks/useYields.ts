import { useState } from "react";
import { investmentApi, type Fund } from "@/services/investmentApi";

/**
 * Yield cache + loading progress (REF-002 Phase 5).
 *
 * Pure move of Index's loadYieldData (~125 lines) plus the four yield
 * states. Success/failure localStorage caches with expiry live here;
 * the verbose emoji debug logs were deleted in the move (error paths
 * surface via toasts at the callers, not the console).
 */
export function useYields() {
  const [fundYields, setFundYields] = useState<Record<number, string>>({});
  const [yieldsLoading, setYieldsLoading] = useState(false);
  const [yieldProgress, setYieldProgress] = useState(0);
  const [loadedYieldRange, setLoadedYieldRange] = useState<number | null>(null);

  const loadYieldData = async (
    fundsData: Fund[],
    limit: number,
    range: number
  ): Promise<void> => {
    setYieldsLoading(true);
    setYieldProgress(0);

    // ponytail: smaller batch lowers burst traffic; increase only if KH rate limit allows.
    const fundBatch = fundsData.slice(0, limit);
    const now = Date.now();
    const twelveHours = 12 * 60 * 60 * 1000;
    const thirtyMinutes = 30 * 60 * 1000; // Shorter cache for failed requests
    const yieldsMap: Record<number, string> = {};

    for (let i = 0; i < fundBatch.length; i++) {
      const fund = fundBatch[i];
      // Check success cache first
      const cacheKey = `yield_${fund.primaryKey}_${range}months`;
      const failCacheKey = `yield_fail_${fund.primaryKey}_${range}months`;
      const cached = localStorage.getItem(cacheKey);
      const failCached = localStorage.getItem(failCacheKey);

      // Skip if recently failed (shorter cache for failures)
      if (failCached) {
        const { timestamp } = JSON.parse(failCached);
        const timeSinceFailure = now - timestamp;
        if (timeSinceFailure < thirtyMinutes) {
          continue;
        } else {
          localStorage.removeItem(failCacheKey);
        }
      }

      // Use cached success if available
      if (cached) {
        const { data, timestamp } = JSON.parse(cached);
        const timeSinceCache = now - timestamp;
        if (timeSinceCache < twelveHours) {
          yieldsMap[fund.primaryKey] = data;
          continue;
        } else {
          localStorage.removeItem(cacheKey);
        }
      }

      // Calculate yield for funds not in cache
      try {
        const yieldPercent = await investmentApi.getSimpleYield(fund.primaryKey, range);

        if (yieldPercent && yieldPercent !== 'null' && yieldPercent !== '0.00%') {
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
      setYieldProgress(((i + 1) / fundBatch.length) * 100);

      // Add small delay to reduce rate limiting
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    setFundYields(yieldsMap);
    setLoadedYieldRange(range);
    setYieldsLoading(false);
  };

  /** Clear the yield map + loaded-range marker (fund list reload). */
  const resetYields = (): void => {
    setFundYields({});
    setLoadedYieldRange(null);
  };

  /** Install a remotely fetched yield map (ERSTE path stops the loader). */
  const applyRemoteYields = (yields: Record<number, string>): void => {
    setFundYields(yields);
    setYieldsLoading(false);
  };

  return {
    fundYields,
    yieldsLoading,
    yieldProgress,
    loadedYieldRange,
    loadYieldData,
    resetYields,
    applyRemoteYields,
  };
}

export type YieldsApi = ReturnType<typeof useYields>;
