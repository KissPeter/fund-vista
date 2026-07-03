import { useState, useEffect } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useToast } from "@/hooks/use-toast";
import { DateRangeFilter } from "@/components/DateRangeFilter";
import { FundCard } from "@/components/FundCard";
import { InvestmentChart, type ReturnAnalysisRow } from "@/components/InvestmentChart";
import { InvestmentsTab } from "@/components/InvestmentsTab";
import { Progress } from "@/components/ui/progress";
import { investmentApi, type Fund, type ChartData } from "@/services/investmentApi";
import { Search, Shield, X } from "lucide-react";

const YIELD_BATCH_SIZE = 10;

const Index = () => {
  const privacyBannerKey = "privacy_banner_dismissed";
  const [provider, setProvider] = useState<"KH" | "ERSTE">("KH");
  const [funds, setFunds] = useState<Fund[]>([]);
  const [filteredFunds, setFilteredFunds] = useState<Fund[]>([]);
  const [fundYields, setFundYields] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedCurrency, setSelectedCurrency] = useState("ALL");
  const [selectedRange, setSelectedRange] = useState(12);
  const [selectedFund, setSelectedFund] = useState<Fund | null>(null);
  const [chartData, setChartData] = useState<ChartData | null>(null);
  const [returnAnalysisRows, setReturnAnalysisRows] = useState<ReturnAnalysisRow[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("funds");
  const [yieldsLoading, setYieldsLoading] = useState(false);
  const [yieldProgress, setYieldProgress] = useState(0);
  const [loadedYieldRange, setLoadedYieldRange] = useState<number | null>(null);
  const [showPrivacyBanner, setShowPrivacyBanner] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    loadFunds();
  }, [provider]);

  useEffect(() => {
    setShowPrivacyBanner(localStorage.getItem(privacyBannerKey) !== "1");
  }, []);

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

  const loadFunds = async (range: number = selectedRange) => {
    try {
      setLoading(true);
      setFunds([]);
      setFilteredFunds([]);
      setFundYields({});
      setLoadedYieldRange(null);
      setSelectedFund(null);
      setChartData(null);
      setReturnAnalysisRows([]);

      if (provider === "ERSTE") {
        const ersteData = await investmentApi.getErsteFunds(range);
        setFunds(ersteData.funds);
        setFilteredFunds(ersteData.funds);
        setFundYields(ersteData.yields);
        setYieldsLoading(false);
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
          loadYieldData(data, YIELD_BATCH_SIZE, range);
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
      setFundYields({});
      loadYieldData(data, YIELD_BATCH_SIZE, range);
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
          setFundYields({});
          loadYieldData(data, YIELD_BATCH_SIZE, range);
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

  const loadYieldData = async (
    fundsData: Fund[],
    limit: number = YIELD_BATCH_SIZE,
    range: number = selectedRange
  ) => {
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
      
      console.log(`🔍 Cache check for fund ${fund.primaryKey} (${range} months):`, {
        cacheKey,
        hasCachedSuccess: !!cached,
        hasCachedFailure: !!failCached
      });
      
      // Skip if recently failed (shorter cache for failures)
      if (failCached) {
        const { timestamp } = JSON.parse(failCached);
        const timeSinceFailure = now - timestamp;
        console.log(`⏱️ Failure cache check for fund ${fund.primaryKey}:`, {
          timestamp,
          timeSinceFailure,
          thirtyMinutes,
          shouldSkip: timeSinceFailure < thirtyMinutes
        });
        if (timeSinceFailure < thirtyMinutes) {
          console.log(`⏭️ Skipping fund ${fund.primaryKey} due to recent failure`);
          continue;
        } else {
          console.log(`🗑️ Removing expired failure cache for fund ${fund.primaryKey}`);
          localStorage.removeItem(failCacheKey);
        }
      }
      
      // Use cached success if available
      if (cached) {
        const { data, timestamp } = JSON.parse(cached);
        const timeSinceCache = now - timestamp;
        console.log(`✅ Success cache check for fund ${fund.primaryKey}:`, {
          data,
          timestamp,
          timeSinceCache,
          twelveHours,
          isValid: timeSinceCache < twelveHours
        });
        if (timeSinceCache < twelveHours) {
          console.log(`🎯 Using cached yield for fund ${fund.primaryKey}: ${data}`);
          yieldsMap[fund.primaryKey] = data;
          continue;
        } else {
          console.log(`🗑️ Removing expired success cache for fund ${fund.primaryKey}`);
          localStorage.removeItem(cacheKey);
        }
      }
      
      // Calculate yield for funds not in cache
      try {
        console.log(`🔍 Calculating yield for fund ${fund.primaryKey} (${fund.portfolioName}) for ${range} months`);
        const yieldPercent = await investmentApi.getSimpleYield(fund.primaryKey, range);
        console.log(`📊 Yield result for fund ${fund.primaryKey}:`, {
          yieldPercent,
          type: typeof yieldPercent,
          isNull: yieldPercent === null,
          isUndefined: yieldPercent === undefined,
          isStringNull: yieldPercent === 'null',
          isZeroPercent: yieldPercent === '0.00%',
          isEmpty: yieldPercent === '',
          actualValue: JSON.stringify(yieldPercent)
        });
        
        if (yieldPercent && yieldPercent !== 'null' && yieldPercent !== '0.00%') {
          console.log(`✅ Accepting yield for fund ${fund.primaryKey}: ${yieldPercent}`);
          yieldsMap[fund.primaryKey] = yieldPercent;
          
          // Cache successful result
          localStorage.setItem(cacheKey, JSON.stringify({
            data: yieldPercent,
            timestamp: now
          }));
        } else {
          console.log(`❌ Rejecting yield for fund ${fund.primaryKey}:`, {
            reason: !yieldPercent ? 'falsy value' : 
                   yieldPercent === 'null' ? 'string "null"' :
                   yieldPercent === '0.00%' ? 'zero percent' : 'unknown',
            value: yieldPercent
          });
          // Cache failure for shorter time
          localStorage.setItem(failCacheKey, JSON.stringify({
            timestamp: now
          }));
        }
      } catch (error) {
        console.error(`💥 Error calculating yield for fund ${fund.primaryKey} (${fund.portfolioName}):`, error);
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

  // Load yields when switching back to funds tab if needed
  useEffect(() => {
    if (
      provider === "KH" &&
      activeTab === "funds" &&
      funds.length > 0 &&
      !yieldsLoading &&
      loadedYieldRange !== selectedRange
    ) {
      loadYieldData(funds, YIELD_BATCH_SIZE, selectedRange);
    }
  }, [provider, activeTab, selectedRange, funds, yieldsLoading, loadedYieldRange]);

  const handleFundClick = async (
    fund: Fund,
    months: number = selectedRange,
    skipProviderGuard: boolean = false
  ) => {
    setSelectedFund(fund);
    setChartLoading(true);
    setActiveTab("analysis");
    
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

  const handleRangeChange = (months: number) => {
    setSelectedRange(months);
    if (provider === "ERSTE" && !selectedFund) {
      loadFunds(months);
      return;
    }
    if (selectedFund) {
      handleFundClick(selectedFund, months);
      return;
    }
    if (funds.length > 0) {
      loadYieldData(funds, YIELD_BATCH_SIZE, months);
    }
  };

  const handleFindTopGainer = () => {
    if (provider === "ERSTE") {
      loadFunds(selectedRange);
      return;
    }
    loadYieldData(funds, funds.length, selectedRange);
  };

  const handleAnalyzeInvestmentFund = async (fundId: number) => {
    try {
      let targetFund = funds.find((fund) => fund.primaryKey === fundId);
      if (!targetFund) {
        const khFunds = await investmentApi.getFunds();
        targetFund = khFunds.find((fund) => fund.primaryKey === fundId);
        if (khFunds.length > 0) {
          setProvider("KH");
          setFunds(khFunds);
          setFilteredFunds(khFunds);
        }
      }

      if (!targetFund) {
        toast({
          title: "Fund not found",
          description: "Could not open analysis for this investment.",
          variant: "destructive",
        });
        return;
      }

      setProvider("KH");
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

  const dismissPrivacyBanner = () => {
    localStorage.setItem(privacyBannerKey, "1");
    setShowPrivacyBanner(false);
  };

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

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted/20 p-4 md:p-6">
      <div className="max-w-7xl mx-auto">
        {showPrivacyBanner && (
          <Alert className="mb-6 border-primary/30 bg-primary/5 text-primary">
            <Shield className="h-4 w-4" />
            <AlertDescription className="pr-8">
              All data is saved only in your browser. Nobody else can read it.
            </AlertDescription>
            <Button
              variant="ghost"
              size="icon"
              onClick={dismissPrivacyBanner}
              className="absolute right-2 top-2 h-7 w-7 text-primary/80 hover:text-primary"
            >
              <X className="h-4 w-4" />
            </Button>
          </Alert>
        )}
        <div className="text-center mb-4">
          <h1 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent mb-2">
            Investment Tracker
          </h1>
          <p className="text-base md:text-lg text-muted-foreground">
            Track and analyze K&H and Erste investment funds
          </p>
        </div>

        <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
          <TabsList className="grid w-full grid-cols-3 max-w-[600px] mx-auto">
            <TabsTrigger value="funds">Controls</TabsTrigger>
            <TabsTrigger value="analysis">Analysis</TabsTrigger>
            <TabsTrigger value="investments">Investments</TabsTrigger>
          </TabsList>

          <TabsContent value="funds" className="space-y-6">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2">
                  <Search className="h-5 w-5" />
                  Search & Filter
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex items-end gap-3 flex-wrap lg:flex-nowrap">
                  <div className="flex flex-wrap items-end gap-3 flex-1 min-w-0">
                    <div className="rounded-md border bg-muted/20 px-3 py-2">
                      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Provider</div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          variant={provider === "KH" ? "default" : "outline"}
                          size="sm"
                          onClick={() => setProvider("KH")}
                        >
                          K&H
                        </Button>
                        <Button
                          variant={provider === "ERSTE" ? "default" : "outline"}
                          size="sm"
                          onClick={() => setProvider("ERSTE")}
                        >
                          ERSTE
                        </Button>
                      </div>
                    </div>

                    <div className="rounded-md border bg-muted/20 px-3 py-2">
                      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Currency</div>
                      <div className="flex flex-wrap gap-2">
                        {currencyButtons.map((currency) => (
                          <Button
                            key={currency}
                            variant={selectedCurrency === currency ? "default" : "outline"}
                            size="sm"
                            onClick={() => setSelectedCurrency(currency)}
                            className="min-w-[56px]"
                          >
                            {currency}
                          </Button>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-md border bg-muted/20 px-3 py-2">
                      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Time</div>
                      <DateRangeFilter
                        selectedRange={selectedRange}
                        onRangeChange={handleRangeChange}
                      />
                    </div>

                    <div className="rounded-md border bg-muted/20 px-3 py-2">
                      <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Search</div>
                      <Input
                        placeholder="Search fund by name or number"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                        className="h-9 w-[220px] bg-background"
                      />
                    </div>
                  </div>

                  <div className="rounded-md border bg-muted/20 px-3 py-2 shrink-0 lg:ml-auto">
                    <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Action</div>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={handleFindTopGainer}
                        disabled={yieldsLoading || funds.length === 0}
                      >
                        Find highest gain
                      </Button>
                    </div>
                  </div>
                </div>

                <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
                  <span>Found {sortedFunds.length}</span>
                  {(loading || yieldsLoading) && (
                    <Badge variant="secondary" className="animate-pulse">
                      {loading ? 'Loading funds...' : `Calculating yields for ${selectedRange} months...`}
                    </Badge>
                  )}
                </div>

                {yieldsLoading && (
                  <div className="mt-3 space-y-2">
                    <div className="flex justify-between text-sm text-muted-foreground">
                      <span>Processing yield calculations...</span>
                      <span>{Math.round(yieldProgress)}%</span>
                    </div>
                    <Progress value={yieldProgress} className="h-2" />
                  </div>
                )}
              </CardContent>
            </Card>

            {loading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {[...Array(6)].map((_, i) => (
                  <Card key={i} className="animate-pulse">
                    <CardHeader>
                      <div className="h-4 bg-muted rounded w-3/4"></div>
                    </CardHeader>
                    <CardContent>
                      <div className="space-y-2">
                        <div className="h-3 bg-muted rounded w-full"></div>
                        <div className="h-3 bg-muted rounded w-2/3"></div>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {sortedFunds.map((fund) => (
                  <FundCard
                    key={fund.primaryKey}
                    fund={fund}
                    yieldPercent={fundYields[fund.primaryKey]}
                    isLoadingYield={yieldsLoading}
                    onClick={() => handleFundClick(fund)}
                  />
                ))}
              </div>
            )}
          </TabsContent>

          <TabsContent value="analysis" className="space-y-6">
            <div className="grid grid-cols-1 gap-6">
            <InvestmentChart 
              data={chartData} 
              loading={chartLoading} 
              selectedFund={selectedFund} 
              returnAnalysisRows={returnAnalysisRows}
              selectedRangeMonths={selectedRange}
              onRangeChange={handleRangeChange}
            />
              
              {selectedFund && (
                <Card>
                  <CardHeader>
                    <CardTitle>Fund Details</CardTitle>
                    <CardDescription>{selectedFund.portfolioName}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                      <div className="space-y-2">
                        <h4 className="font-medium">Basic Info</h4>
                        <div className="text-sm space-y-1">
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Fund No:</span>
                            <span className="font-mono">{selectedFund.fundNo}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Currency:</span>
                            <span>{selectedFund.currencyType}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Type:</span>
                            <span>{selectedFund.investmentType}</span>
                          </div>
                        </div>
                      </div>
                      
                      <div className="space-y-2">
                        <h4 className="font-medium">Investment Options</h4>
                        <div className="flex flex-wrap gap-1">
                          {selectedFund.regularityTypes.map((type) => (
                            <Badge key={type} variant="outline">
                              {type === 'ONETIME' ? 'One-time' : 'Regular'}
                            </Badge>
                          ))}
                        </div>
                      </div>
                      
                      <div className="space-y-2">
                        <h4 className="font-medium">Characteristics</h4>
                        <div className="space-y-1">
                          <Badge variant="outline">
                            {selectedFund.deviceClassType.replace('_', ' ')}
                          </Badge>
                          <br />
                          <Badge 
                            variant="outline"
                            className={selectedFund.sustainabilityType === 'RESPONSIBLE_FUTURE' 
                              ? 'bg-green-100 text-green-800 border-green-300' 
                              : ''}
                          >
                            {selectedFund.sustainabilityType === 'RESPONSIBLE_FUTURE' ? 'ESG Focused' : 'Traditional'}
                          </Badge>
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}
            </div>
          </TabsContent>

          <TabsContent value="investments" className="space-y-6">
            <InvestmentsTab onAnalyzeFund={handleAnalyzeInvestmentFund} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
};

export default Index;
