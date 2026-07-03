import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Download, Upload, TrendingDown, AlertCircle, Trash2 } from "lucide-react";
import { investmentStorage } from "@/services/investmentStorage";
import { investmentApi } from "@/services/investmentApi";
import { exchangeRateApi } from "@/services/exchangeRateApi";
import type { Investment, InvestmentCalculation } from "@/types/investment";
import { useToast } from "@/hooks/use-toast";

interface InvestmentsTabProps {
  onAnalyzeFund?: (fundId: number) => void;
}

export const InvestmentsTab = ({ onAnalyzeFund }: InvestmentsTabProps) => {
  const [investments, setInvestments] = useState<Investment[]>([]);
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();
  const formatHuf = (value: number) =>
    `${value.toLocaleString("hu-HU", { maximumFractionDigits: 0 })} HUF`;

  useEffect(() => {
    loadInvestments();
  }, []);

  const getInvestmentKey = (investment: Investment) =>
    investment.id || `${investment.fundId}-${investment.investmentDate}-${investment.amount}`;

  const loadInvestments = async () => {
    setLoading(true);
    const storedInvestments = investmentStorage.getInvestments();
    
    // Update current fund values for unsold investments
    const updatedInvestments = await Promise.all(
      storedInvestments.map(async (investment) => {
        if (investment.sold) {
          return investment;
        }
        
        try {
          const data = await investmentApi.getCalculationData(investment.fundId);
          const currentValue = data.diagram?.series?.[0]?.values?.slice(-1)[0];
          const currency = (investment.fundCurrency || "HUF").toUpperCase();
          const fxRateToHufAtInvestment = investment.fxRateToHufAtInvestment
            ?? await exchangeRateApi.getRateToHuf(currency, investment.investmentDate);
          const currentFxRateToHuf = investment.sold
            ? (investment.soldFxRateToHuf ?? investment.currentFxRateToHuf ?? await exchangeRateApi.getRateToHuf(currency))
            : await exchangeRateApi.getRateToHuf(currency);

          return {
            ...investment,
            fundCurrency: currency,
            fxRateToHufAtInvestment,
            amountInFundCurrency: investment.amountInFundCurrency ?? (investment.amount / fxRateToHufAtInvestment),
            currentFxRateToHuf,
            currentFundValue: currentValue || investment.currentFundValue
          };
        } catch (error) {
          return investment;
        }
      })
    );

    investmentStorage.replaceInvestments(updatedInvestments);
    setInvestments(updatedInvestments);
    setLoading(false);
  };

  const calculateInvestment = (investment: Investment): InvestmentCalculation => {
    const currentValue = investment.sold ? investment.soldFundValue! : investment.currentFundValue!;
    const fxAtInvestment = investment.fxRateToHufAtInvestment ?? 1;
    const currentFx = investment.sold
      ? (investment.soldFxRateToHuf ?? investment.currentFxRateToHuf ?? 1)
      : (investment.currentFxRateToHuf ?? 1);
    const amountInFundCurrency = investment.amountInFundCurrency ?? (investment.amount / fxAtInvestment);
    const shares = amountInFundCurrency / investment.fundValueAtInvestment;
    const grossValue = shares * currentValue;
    const grossValueHuf = grossValue * currentFx;
    const grossProfit = grossValueHuf - investment.amount;
    const grossProfitPercent = (grossProfit / investment.amount) * 100;
    const netValue = grossValueHuf;
    const netProfit = netValue - investment.amount;
    const netProfitPercent = (netProfit / investment.amount) * 100;
    
    return {
      grossValue: grossValueHuf,
      netValue,
      grossProfit,
      netProfit,
      grossProfitPercent,
      netProfitPercent
    };
  };

  const handleSell = async (investmentId: string) => {
    const investment = investments.find(inv => inv.id === investmentId);
    if (!investment || investment.sold) return;
    try {
      const currency = (investment.fundCurrency || "HUF").toUpperCase();
      const soldFxRateToHuf = await exchangeRateApi.getRateToHuf(currency);

      const updates = {
        sold: true,
        soldDate: new Date().toISOString().split('T')[0],
        soldFundValue: investment.currentFundValue,
        soldFxRateToHuf,
      };

      investmentStorage.updateInvestment(investmentId, updates);
      setInvestments(prev => 
        prev.map(inv => 
          inv.id === investmentId ? { ...inv, ...updates } : inv
        )
      );

      toast({
        title: "Investment Sold",
        description: "Investment has been marked as sold",
      });
    } catch (error) {
      toast({
        title: "FX error",
        description: "Could not fetch current exchange rate for selling.",
        variant: "destructive",
      });
      console.error("Failed to mark investment as sold:", error);
    }
  };

  const handleRevertSell = (investmentId: string) => {
    const investment = investments.find(inv => inv.id === investmentId);
    if (!investment || !investment.sold) return;

    const updates = {
      sold: false,
      soldDate: undefined,
      soldFundValue: undefined
    };

    investmentStorage.updateInvestment(investmentId, updates);
    setInvestments(prev =>
      prev.map(inv =>
        inv.id === investmentId ? { ...inv, ...updates } : inv
      )
    );

    toast({
      title: "Sale Reverted",
      description: "Investment is active again",
    });
  };

  const handleDeleteInvestment = (investment: Investment) => {
    if (!investment.sold) return;
    const updated = investments.map((inv) =>
      getInvestmentKey(inv) === getInvestmentKey(investment)
        ? { ...inv, hidden: true }
        : inv
    );
    investmentStorage.replaceInvestments(updated);
    setInvestments(updated);

    toast({
      title: "Investment Hidden",
      description: "Sold investment hidden from this list",
    });
  };

  const handleExport = () => {
    const data = investmentStorage.exportInvestments();
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `investments_${new Date().toISOString().split('T')[0]}.json`;
    a.click();
    URL.revokeObjectURL(url);
    
    toast({
      title: "Export Complete",
      description: "Investments exported successfully",
    });
  };

  const handleImport = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const jsonData = e.target?.result as string;
        if (investmentStorage.importInvestments(jsonData)) {
          loadInvestments();
          toast({
            title: "Import Complete",
            description: "Investments imported successfully",
          });
        } else {
          throw new Error("Invalid file format");
        }
      } catch (error) {
        toast({
          title: "Import Failed",
          description: "Failed to import investments. Please check the file format.",
          variant: "destructive",
        });
      }
    };
    reader.readAsText(file);
    
    // Reset input
    event.target.value = '';
  };

  const visibleInvestments = investments.filter(
    (investment) => !investment.hidden
  );
  const activeInvestments = visibleInvestments.filter((investment) => !investment.sold);

  const totalPortfolio = activeInvestments.reduce((total, investment) => {
    const calc = calculateInvestment(investment);
    return total + calc.netValue;
  }, 0);

  const totalInvested = activeInvestments.reduce((total, investment) => total + investment.amount, 0);
  const totalNetProfit = totalPortfolio - totalInvested;
  const totalNetProfitPercent = totalInvested > 0 ? (totalNetProfit / totalInvested) * 100 : 0;

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold">My Investments</h2>
          <p className="text-muted-foreground">Track your investment portfolio</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={handleExport}>
            <Download className="mr-2 h-4 w-4" />
            Export
          </Button>
          <label className="cursor-pointer">
            <Button variant="outline" asChild>
              <span>
                <Upload className="mr-2 h-4 w-4" />
                Import
              </span>
            </Button>
            <Input
              type="file"
              accept=".json"
              onChange={handleImport}
              className="hidden"
            />
          </label>
        </div>
      </div>

      {/* Portfolio Summary */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Total Invested</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatHuf(totalInvested)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Portfolio Value (Net)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatHuf(totalPortfolio)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Net Profit</CardTitle>
          </CardHeader>
          <CardContent>
            <div className={`text-2xl font-bold ${totalNetProfit >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
              {totalNetProfit >= 0 ? "+" : ""}{formatHuf(totalNetProfit)}
            </div>
            <div className={`text-sm ${totalNetProfitPercent >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
              {totalNetProfitPercent >= 0 ? '+' : ''}{totalNetProfitPercent.toFixed(2)}%
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Investments Table */}
      <Card>
        <CardHeader>
          <CardTitle>Investment Details</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
            </div>
          ) : visibleInvestments.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              <AlertCircle className="mx-auto h-12 w-12 mb-4 opacity-50" />
              <p>No investments found. Start by adding an investment from the Analysis tab.</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse border border-border">
                <thead>
                  <tr className="bg-muted/50">
                    <th className="border border-border p-3 text-left">Fund</th>
                    <th className="border border-border p-3 text-right">Amount (HUF)</th>
                    <th className="border border-border p-3 text-right">Date</th>
                    <th className="border border-border p-3 text-right">Gross Value</th>
                    <th className="border border-border p-3 text-right">Net Value</th>
                    <th className="border border-border p-3 text-right">Net Profit</th>
                    <th className="border border-border p-3 text-center">Status</th>
                    <th className="border border-border p-3 text-center">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleInvestments.map((investment) => {
                    const calc = calculateInvestment(investment);
                    const isPositive = calc.netProfit >= 0;
                    
                    return (
                      <tr key={getInvestmentKey(investment)}>
                        <td className="border border-border p-3">
                          {onAnalyzeFund ? (
                            <button
                              type="button"
                              className="text-primary underline-offset-2 hover:underline"
                              onClick={() => onAnalyzeFund(investment.fundId)}
                            >
                              {investment.fundName}
                            </button>
                          ) : (
                            investment.fundName
                          )}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {formatHuf(investment.amount)}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {investment.investmentDate}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {formatHuf(calc.grossValue)}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {formatHuf(calc.netValue)}
                        </td>
                        <td className={`border border-border p-3 text-right font-semibold ${
                          isPositive ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                        }`}>
                          {isPositive ? "+" : ""}{formatHuf(calc.netProfit)}
                          <div className="text-xs font-normal">
                            {isPositive ? '+' : ''}{calc.netProfitPercent.toFixed(2)}%
                          </div>
                        </td>
                        <td className="border border-border p-3 text-center">
                          <Badge variant={investment.sold ? "secondary" : "default"}>
                            {investment.sold ? "Sold" : "Active"}
                          </Badge>
                        </td>
                        <td className="border border-border p-3 text-center">
                          {!investment.sold ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleSell(investment.id)}
                            >
                              <TrendingDown className="mr-1 h-3 w-3" />
                              Sell
                            </Button>
                          ) : (
                            <div className="flex items-center justify-center gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => handleRevertSell(investment.id)}
                              >
                                Revert sell
                              </Button>
                              <Button
                                variant="outline"
                                size="icon"
                                onClick={() => handleDeleteInvestment(investment)}
                                aria-label="Delete sold investment"
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
};