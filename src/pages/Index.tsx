import { useState, useEffect } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/hooks/use-toast";
import { DateRangeFilter } from "@/components/DateRangeFilter";
import { FundCard } from "@/components/FundCard";
import { InvestmentChart } from "@/components/InvestmentChart";
import { Progress } from "@/components/ui/progress";
import { investmentApi, type Fund, type ChartData } from "@/services/investmentApi";
import { Search } from "lucide-react";

const Index = () => {
  const [funds, setFunds] = useState<Fund[]>([]);
  const [filteredFunds, setFilteredFunds] = useState<Fund[]>([]);
  const [fundYields, setFundYields] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedRange, setSelectedRange] = useState(12);
  const [selectedFund, setSelectedFund] = useState<Fund | null>(null);
  const [chartData, setChartData] = useState<ChartData | null>(null);
  const [chartLoading, setChartLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("funds");
  const [yieldsLoading, setYieldsLoading] = useState(false);
  const [yieldProgress, setYieldProgress] = useState(0);
  const { toast } = useToast();

  useEffect(() => {
    loadFunds();
  }, []);

  useEffect(() => {
    const filtered = funds.filter(fund =>
      fund.portfolioName.toLowerCase().includes(searchTerm.toLowerCase()) ||
      fund.fundNo.toLowerCase().includes(searchTerm.toLowerCase())
    );
    setFilteredFunds(filtered);
  }, [funds, searchTerm]);

  const loadFunds = async () => {
    try {
      setLoading(true);
      
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
          loadYieldData(data);
          setLoading(false);
          return;
        }
      }
      
      // Fetch fresh data
      const data = await investmentApi.getFunds();
      
      // Cache the data
      localStorage.setItem(cacheKey, JSON.stringify({
        data,
        timestamp: now
      }));
      
      setFunds(data);
      setFilteredFunds(data);
      loadYieldData(data);
    } catch (error) {
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

  const loadYieldData = async (fundsData: Fund[]) => {
    setYieldsLoading(true);
    setYieldProgress(0);
    
    // Load yield data for first 20 funds to avoid rate limiting
    const fundBatch = fundsData.slice(0, 20);
    const now = Date.now();
    const twelveHours = 12 * 60 * 60 * 1000;
    const thirtyMinutes = 30 * 60 * 1000; // Shorter cache for failed requests
    const yieldsMap: Record<number, string> = {};
    
    for (let i = 0; i < fundBatch.length; i++) {
      const fund = fundBatch[i];
      // Check success cache first
      const cacheKey = `yield_${fund.primaryKey}_${selectedRange}months`;
      const failCacheKey = `yield_fail_${fund.primaryKey}_${selectedRange}months`;
      const cached = localStorage.getItem(cacheKey);
      const failCached = localStorage.getItem(failCacheKey);
      
      // Skip if recently failed (shorter cache for failures)
      if (failCached) {
        const { timestamp } = JSON.parse(failCached);
        if (now - timestamp < thirtyMinutes) {
          continue;
        } else {
          localStorage.removeItem(failCacheKey);
        }
      }
      
      // Use cached success if available
      if (cached) {
        const { data, timestamp } = JSON.parse(cached);
        if (now - timestamp < twelveHours) {
          yieldsMap[fund.primaryKey] = data;
          continue;
        }
      }
      
      // Calculate yield for funds not in cache
      try {
        const yieldPercent = await investmentApi.getSimpleYield(fund.primaryKey, selectedRange);
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
      } catch (error) {
        console.warn(`Failed to load yield for fund ${fund.primaryKey}:`, error);
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
    setYieldsLoading(false);
  };

  // Load yields when switching back to funds tab if needed
  useEffect(() => {
    if (activeTab === "funds" && funds.length > 0) {
      // Check if we have yields for the current timeframe
      const hasYieldsForCurrentTimeframe = funds.slice(0, 20).some(fund => fundYields[fund.primaryKey]);
      if (!hasYieldsForCurrentTimeframe) {
        loadYieldData(funds);
      }
    }
  }, [activeTab, selectedRange]);

  const handleFundClick = async (fund: Fund) => {
    setSelectedFund(fund);
    setChartLoading(true);
    setActiveTab("analysis");
    
    try {
      const data = await investmentApi.getCalculationData(
        fund.primaryKey, 
        50000, 
        selectedRange, 
        fund.regularityTypes.includes('REGULAR') ? 'REGULAR' : 'ONETIME'
      );
      setChartData(data);
    } catch (error) {
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
    if (selectedFund) {
      handleFundClick(selectedFund);
    }
    // Recalculate yields for current timeframe
    if (funds.length > 0) {
      loadYieldData(funds);
    }
  };

  // Sort funds by yield percentage (descending)
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
    <div className="min-h-screen bg-gradient-to-br from-background to-muted/20 p-4 md:p-8">
      <div className="max-w-7xl mx-auto">
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent mb-4">
            Investment Tracker
          </h1>
          <p className="text-xl text-muted-foreground">
            Track and analyze K&H investment funds performance
          </p>
        </div>

        <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-6">
          <TabsList className="grid w-full grid-cols-2 max-w-[400px] mx-auto">
            <TabsTrigger value="funds">Fund Explorer</TabsTrigger>
            <TabsTrigger value="analysis">Analysis</TabsTrigger>
          </TabsList>

          <TabsContent value="funds" className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Search className="h-5 w-5" />
                  Search & Filter
                </CardTitle>
                <CardDescription>
                  Find investment funds and analyze their performance
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex flex-col sm:flex-row gap-4">
                  <div className="flex-1">
                    <Input
                      placeholder="Search by fund name or number..."
                      value={searchTerm}
                      onChange={(e) => setSearchTerm(e.target.value)}
                      className="w-full"
                    />
                  </div>
                  <DateRangeFilter 
                    selectedRange={selectedRange}
                    onRangeChange={handleRangeChange}
                  />
                </div>
                
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <span>Found {sortedFunds.length} funds</span>
                  {(loading || yieldsLoading) && (
                    <Badge variant="secondary" className="animate-pulse">
                      {loading ? 'Loading funds...' : `Calculating yields for ${selectedRange} months...`}
                    </Badge>
                  )}
                </div>
                
                {yieldsLoading && (
                  <div className="space-y-2">
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
              <InvestmentChart data={chartData} loading={chartLoading} />
              
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
        </Tabs>
      </div>
    </div>
  );
};

export default Index;
