import { useEffect, useState } from "react";
import { investmentApi, type Fund } from "@/services/investmentApi";
import { useToast } from "@/hooks/use-toast";
import { readJsonCache } from "@/services/jsonCache";
import type { YieldsApi } from "@/hooks/useYields";

export type Provider = "KH" | "ERSTE";

/** Shape of the cached fund list: the funds plus the write time. */
interface FundCacheEntry {
  data: Fund[];
  timestamp: number;
}

const isFundCacheEntry = (value: unknown): value is FundCacheEntry =>
  typeof value === "object" &&
  value !== null &&
  Array.isArray((value as FundCacheEntry).data) &&
  typeof (value as FundCacheEntry).timestamp === "number" &&
  Number.isFinite((value as FundCacheEntry).timestamp);

interface FundsDeps {
  loadYieldData: YieldsApi["loadYieldData"];
  applyRemoteYields: YieldsApi["applyRemoteYields"];
  resetYields: YieldsApi["resetYields"];
  fundYields: Record<number, string>;
  /** Clear the chart/detail selection (owned by useFundChart). */
  resetDetail: () => void;
}

/**
 * Fund list + filter state (REF-002 Phase 5).
 *
 * Pure move of Index's fund loading, search/currency/range filter, and
 * yield-sorted list. Takes the provider as an argument (the switch UI
 * stays in Index) plus the yields/chart collaborators it orchestrates.
 */
export function useFunds(provider: Provider, deps: FundsDeps) {
  const [funds, setFunds] = useState<Fund[]>([]);
  const [filteredFunds, setFilteredFunds] = useState<Fund[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedCurrency, setSelectedCurrency] = useState("ALL");
  const [selectedRange, setSelectedRange] = useState(12);
  const { toast } = useToast();

  const { loadYieldData, applyRemoteYields, resetYields, fundYields, resetDetail } = deps;

  /**
   * Start a yield load without letting its rejection escape (#96).
   *
   * `loadYieldData` rejects when something inside the load throws (a full
   * localStorage, a failing API call). An unhandled rejection at the call
   * site used to leave the dashboard with a stuck spinner and a console
   * full of errors, so the failure is caught and shown as a toast here.
   */
  const startYieldLoad = (data: Fund[], range: number): void => {
    loadYieldData(data, data.length, range).catch((error) => {
      console.error('Failed to load yields:', error);
      toast({
        title: "Error",
        description: "Failed to load yields.",
        variant: "destructive",
      });
    });
  };

  const loadFunds = async (range: number = selectedRange): Promise<void> => {
    try {
      setLoading(true);
      setFunds([]);
      setFilteredFunds([]);
      resetYields();
      resetDetail();

      if (provider === "ERSTE") {
        const ersteData = await investmentApi.getErsteFunds(range);
        setFunds(ersteData.funds);
        setFilteredFunds(ersteData.funds);
        applyRemoteYields(ersteData.yields);
        return;
      }

      // Check cache first
      const cacheKey = 'investment_funds_cache';
      const now = Date.now();

      // A corrupt cache entry is a cache miss, not a throw (#96).
      const cached = readJsonCache<FundCacheEntry>(cacheKey, isFundCacheEntry);

      if (cached) {
        const oneDay = 24 * 60 * 60 * 1000;

        if (now - cached.timestamp < oneDay) {
          setFunds(cached.data);
          setFilteredFunds(cached.data);
          startYieldLoad(cached.data, range);
          setLoading(false);
          return;
        }
      }

      // Fetch fresh data
      const data = await investmentApi.getFunds();

      if (!Array.isArray(data) || data.length === 0) {
        throw new Error("KH fund list is temporarily empty (likely throttled)");
      }

      // Cache the data
      localStorage.setItem(cacheKey, JSON.stringify({
        data,
        timestamp: now
      }));

      setFunds(data);
      setFilteredFunds(data);
      startYieldLoad(data, range);
    } catch (error) {
      if (provider === "ERSTE") {
        toast({
          title: "Error",
          description: "Failed to load Erste funds.",
          variant: "destructive",
        });
        return;
      }
      // The fallback cache read is tolerant too: a corrupt entry is a miss (#96).
      const fallbackCached = readJsonCache<FundCacheEntry>('investment_funds_cache', isFundCacheEntry);
      if (fallbackCached && Array.isArray(fallbackCached.data) && fallbackCached.data.length > 0) {
        setFunds(fallbackCached.data);
        setFilteredFunds(fallbackCached.data);
        startYieldLoad(fallbackCached.data, range);
        toast({
          title: "Using cached funds",
          description: "Live source is rate-limited right now.",
        });
        return;
      }

      toast({
        title: "Error",
        description: "Failed to load funds. Please check if the API is accessible.",
        variant: "destructive",
      });
      console.error('Failed to load funds:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadFunds();
  }, [provider]);

  useEffect(() => {
    const filtered = funds.filter(fund =>
      (
        fund.portfolioName.toLowerCase().includes(searchTerm.toLowerCase()) ||
        fund.fundNo.toLowerCase().includes(searchTerm.toLowerCase())
      ) &&
      (selectedCurrency === "ALL" || fund.currencyType === selectedCurrency)
    );
    setFilteredFunds(filtered);
  }, [funds, searchTerm, selectedCurrency]);

  useEffect(() => {
    setSelectedCurrency("ALL");
  }, [provider]);

  // Sort funds by yield percentage (descending)
  const currencyButtons = ["ALL", "HUF", "EUR", "USD"];

  const sortedFunds = [...filteredFunds].sort((a, b) => {
    const yieldA = fundYields[a.primaryKey];
    const yieldB = fundYields[b.primaryKey];

    if (!yieldA && !yieldB) return 0;
    if (!yieldA) return 1;
    if (!yieldB) return -1;

    const percentA = parseFloat(yieldA.replace(',', '.').replace('%', ''));
    const percentB = parseFloat(yieldB.replace(',', '.').replace('%', ''));

    return percentB - percentA;
  });

  return {
    funds,
    filteredFunds,
    sortedFunds,
    loading,
    searchTerm,
    setSearchTerm,
    selectedCurrency,
    setSelectedCurrency,
    selectedRange,
    setSelectedRange,
    loadFunds,
    currencyButtons,
  };
}

export type FundsApi = ReturnType<typeof useFunds>;
