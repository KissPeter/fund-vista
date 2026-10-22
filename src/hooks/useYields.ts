import { useState } from "react";
import { investmentApi, type Fund } from "@/services/investmentApi";
import { loadYields } from "@/services/yieldCache";

/**
 * Yield cache + loading progress (REF-002 Phase 5).
 *
 * Pure move of Index's loadYieldData (~125 lines) plus the four yield
 * states. Success/failure localStorage caches with expiry live in
 * `@/services/yieldCache`; the verbose emoji debug logs were deleted in
 * the move (error paths surface via toasts at the callers, not the
 * console).
 *
 * The cache reads are tolerant (#96): a truncated or hand-edited
 * localStorage entry must be a cache miss, never a throw - a throw here
 * escaped the async function, the loading flag was never reset and the
 * dashboard stayed stuck on "loading yields" forever.
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
    await loadYields(fundsData, limit, range, {
      fetchYield: (primaryKey, months) => investmentApi.getSimpleYield(primaryKey, months),
      setLoading: setYieldsLoading,
      setProgress: setYieldProgress,
      onLoaded: (yields, loadedRange) => {
        setFundYields(yields);
        setLoadedYieldRange(loadedRange);
      },
    });
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
