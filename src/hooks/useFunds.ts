import { useEffect, useState } from "react";
import { investmentApi, type Fund } from "@/services/investmentApi";
import { useToast } from "@/hooks/use-toast";
import type { YieldsApi } from "@/hooks/useYields";

export type Provider = "KH" | "ERSTE";

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
      const cached = localStorage.getItem(cacheKey);
      const now = Date.now();

      if (cached) {
        const { data, timestamp } = JSON.parse(cached);
        const oneDay = 24 * 60 * 60 * 1000;

        if (now - timestamp < oneDay) {
          setFunds(data);
          setFilteredFunds(data);
          loadYieldData(data, data.length, range);
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
      loadYieldData(data, data.length, range);
    } catch (error) {
      if (provider === "ERSTE") {
        toast({
          title: "Error",
          description: "Failed to load Erste funds.",
          variant: "destructive",
        });
        return;
      }
      const fallbackCached = localStorage.getItem('investment_funds_cache');
      if (fallbackCached) {
        const { data } = JSON.parse(fallbackCached);
        if (Array.isArray(data) && data.length > 0) {
          setFunds(data);
          setFilteredFunds(data);
          loadYieldData(data, data.length, range);
          toast({
            title: "Using cached funds",
            description: "Live source is rate-limited right now.",
          });
          return;
        }
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
