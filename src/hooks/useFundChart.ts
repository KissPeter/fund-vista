import { useState } from "react";
import { investmentApi, type ChartData, type Fund } from "@/services/investmentApi";
import type { ReturnAnalysisRow } from "@/components/InvestmentChart";
import { useToast } from "@/hooks/use-toast";
import type { Provider } from "@/hooks/useFunds";

interface ChartDeps {
  provider: Provider;
  /** Switch to the analysis tab (owned by Index). */
  onShowAnalysis: () => void;
}

/**
 * Fund detail + chart + investment-analysis actions (REF-002 Phase 5).
 *
 * Pure move of Index's handleFundClick (~80 lines), the chart states,
 * and handleAnalyzeInvestmentFund. Console error reporting in the
 * failure paths is kept (genuine error paths, already toasted).
 */
export function useFundChart(deps: ChartDeps) {
  const [selectedFund, setSelectedFund] = useState<Fund | null>(null);
  const [chartData, setChartData] = useState<ChartData | null>(null);
  const [returnAnalysisRows, setReturnAnalysisRows] = useState<ReturnAnalysisRow[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const { toast } = useToast();

  const { provider, onShowAnalysis } = deps;

  const handleFundClick = async (
    fund: Fund,
    months: number,
    skipProviderGuard: boolean = false
  ): Promise<void> => {
    setSelectedFund(fund);
    setChartLoading(true);
    onShowAnalysis();

    try {
      const regularityType = fund.regularityTypes.includes('REGULAR') ? 'REGULAR' : 'ONETIME';
      const getPeriodData = async (periodMonths: number) => {
        if (provider === "ERSTE" && !skipProviderGuard) {
          return investmentApi.getErsteCalculationData(fund.primaryKey, periodMonths);
        }
        return investmentApi.getCalculationData(
          fund.primaryKey,
          50000,
          periodMonths,
          regularityType
        );
      };

      const data = await getPeriodData(months);
      setChartData(data);

      const periods = [
        { label: "1 Month", months: 1 },
        { label: "3 Months", months: 3 },
        { label: "6 Months", months: 6 },
        { label: "12 Months", months: 12 },
      ];

      const toRow = (label: string, periodData: ChartData): ReturnAnalysisRow => {
        const values = periodData.diagram?.series?.[0]?.values ?? [];
        if (values.length < 2) {
          return { label, returnPercent: null, startValue: null, endValue: null };
        }
        const startValue = values[0];
        const endValue = values[values.length - 1];
        if (!startValue || !endValue) {
          return { label, returnPercent: null, startValue: null, endValue: null };
        }

        return {
          label,
          returnPercent: ((endValue - startValue) / Math.abs(startValue)) * 100,
          startValue,
          endValue,
        };
      };

      // ponytail: use existing API cache so this stays cheap after first load.
      const analysisRows = await Promise.all(
        periods.map(async (period) => {
          try {
            const periodData = period.months === months
              ? data
              : await getPeriodData(period.months);
            return toRow(period.label, periodData);
          } catch {
            return { label: period.label, returnPercent: null, startValue: null, endValue: null };
          }
        })
      );
      setReturnAnalysisRows(analysisRows);
    } catch (error) {
      setReturnAnalysisRows([]);
      toast({
        title: "Error",
        description: "Failed to load chart data for this fund.",
        variant: "destructive",
      });
      console.error('Failed to load chart data:', error);
    } finally {
      setChartLoading(false);
    }
  };

  const handleAnalyzeInvestmentFund = async (
    fundId: number,
    funds: Fund[],
    selectedRange: number
  ): Promise<void> => {
    try {
      let targetFund = funds.find((fund) => fund.primaryKey === fundId);
      if (!targetFund) {
        const khFunds = await investmentApi.getFunds();
        targetFund = khFunds.find((fund) => fund.primaryKey === fundId);
      }

      if (!targetFund) {
        toast({
          title: "Fund not found",
          description: "Could not open analysis for this investment.",
          variant: "destructive",
        });
        return;
      }

      await handleFundClick(targetFund, selectedRange, true);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to open investment analysis.",
        variant: "destructive",
      });
      console.error("Failed to open investment analysis:", error);
    }
  };

  /** Clear the detail selection (called when the fund list reloads). */
  const resetDetail = (): void => {
    setSelectedFund(null);
    setChartData(null);
    setReturnAnalysisRows([]);
  };

  return {
    selectedFund,
    chartData,
    returnAnalysisRows,
    chartLoading,
    handleFundClick,
    handleAnalyzeInvestmentFund,
    resetDetail,
  };
}

export type FundChartApi = ReturnType<typeof useFundChart>;
