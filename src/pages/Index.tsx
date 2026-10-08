import { useState, useEffect } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { InvestmentsTab } from "@/components/InvestmentsTab";
import { useFunds, type Provider } from "@/hooks/useFunds";
import { useYields } from "@/hooks/useYields";
import { useFundChart } from "@/hooks/useFundChart";
import { PrivacyBanner } from "@/components/dashboard/PrivacyBanner";
import { FilterBar } from "@/components/dashboard/FilterBar";
import { FundGrid } from "@/components/dashboard/FundGrid";
import { ChartPanel } from "@/components/dashboard/ChartPanel";

/**
 * Fund-vista dashboard page (REF-002 Phase 5).
 *
 * Tabs layout + provider switch only. Fund list/filter state lives in
 * useFunds, yield cache + progress in useYields, detail/chart actions in
 * useFundChart; the tab bodies are dashboard components. InvestmentApi
 * shapes are untouched (services/investmentApi.ts is out of scope).
 */
const Index = () => {
  const [provider, setProvider] = useState<Provider>("KH");
  const [activeTab, setActiveTab] = useState("funds");

  const yields = useYields();
  const chart = useFundChart({
    provider,
    onShowAnalysis: () => setActiveTab("analysis"),
  });
  const funds = useFunds(provider, {
    loadYieldData: yields.loadYieldData,
    applyRemoteYields: yields.applyRemoteYields,
    resetYields: yields.resetYields,
    fundYields: yields.fundYields,
    resetDetail: chart.resetDetail,
  });

  // Load yields when switching back to funds tab if needed
  useEffect(() => {
    if (
      provider === "KH" &&
      activeTab === "funds" &&
      funds.funds.length > 0 &&
      !yields.yieldsLoading &&
      yields.loadedYieldRange !== funds.selectedRange
    ) {
      yields.loadYieldData(funds.funds, funds.funds.length, funds.selectedRange);
    }
  }, [provider, activeTab, funds.selectedRange, funds.funds, yields.yieldsLoading, yields.loadedYieldRange]);

  const handleRangeChange = (months: number): void => {
    funds.setSelectedRange(months);
    if (chart.selectedFund && activeTab === "analysis") {
      chart.handleFundClick(chart.selectedFund, months);
      return;
    }
    if (provider === "ERSTE") {
      funds.loadFunds(months);
      return;
    }
    if (funds.funds.length > 0) {
      yields.loadYieldData(funds.funds, funds.funds.length, months);
    }
  };

  const handleFindTopGainer = (): void => {
    if (provider === "ERSTE") {
      funds.loadFunds(funds.selectedRange);
      return;
    }
    yields.loadYieldData(funds.funds, funds.funds.length, funds.selectedRange);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted/20 p-4 md:p-6">
      <div className="max-w-7xl mx-auto">
        <PrivacyBanner />
        <div className="text-center mb-4">
          <h1 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent mb-2">
            Investment Tracker
          </h1>
          <p className="text-base md:text-lg text-muted-foreground">
            Track and analyze K&H and Erste investment funds
          </p>
        </div>

        <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
          <TabsList className="grid w-full grid-cols-3 max-w-[800px] mx-auto">
            <TabsTrigger value="funds">Controls</TabsTrigger>
            <TabsTrigger value="analysis">Analysis</TabsTrigger>
            <TabsTrigger value="investments">Investments</TabsTrigger>
          </TabsList>

          <TabsContent value="funds" className="space-y-6">
            <FilterBar
              searchTerm={funds.searchTerm}
              onSearchChange={funds.setSearchTerm}
              provider={provider}
              onProviderChange={setProvider}
              selectedCurrency={funds.selectedCurrency}
              onCurrencyChange={funds.setSelectedCurrency}
              currencyButtons={funds.currencyButtons}
              selectedRange={funds.selectedRange}
              onRangeChange={handleRangeChange}
              onFindTopGainer={handleFindTopGainer}
              actionDisabled={yields.yieldsLoading || funds.funds.length === 0}
              foundCount={funds.sortedFunds.length}
              loading={funds.loading}
              yieldsLoading={yields.yieldsLoading}
              yieldProgress={yields.yieldProgress}
            />
            <FundGrid
              loading={funds.loading}
              funds={funds.sortedFunds}
              fundYields={yields.fundYields}
              yieldsLoading={yields.yieldsLoading}
              onFundClick={(fund) => chart.handleFundClick(fund, funds.selectedRange)}
            />
          </TabsContent>

          <TabsContent value="analysis" className="space-y-6">
            <ChartPanel
              chartData={chart.chartData}
              chartLoading={chart.chartLoading}
              selectedFund={chart.selectedFund}
              returnAnalysisRows={chart.returnAnalysisRows}
              selectedRange={funds.selectedRange}
              onRangeChange={handleRangeChange}
            />
          </TabsContent>

          <TabsContent value="investments" className="space-y-6">
            <InvestmentsTab
              onAnalyzeFund={(fundId) =>
                chart.handleAnalyzeInvestmentFund(
                  fundId,
                  funds.funds,
                  funds.selectedRange
                )
              }
            />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
};

export default Index;
