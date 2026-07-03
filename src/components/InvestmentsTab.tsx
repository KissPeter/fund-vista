import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Download, Upload, TrendingDown, AlertCircle } from "lucide-react";
import { investmentStorage } from "@/services/investmentStorage";
import { investmentApi } from "@/services/investmentApi";
import type { Investment, InvestmentCalculation } from "@/types/investment";
import { useToast } from "@/hooks/use-toast";

export const InvestmentsTab = () => {
  const [investments, setInvestments] = useState<Investment[]>([]);
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    loadInvestments();
  }, []);

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
          return {
            ...investment,
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
    const shares = investment.amount / investment.fundValueAtInvestment;
    const grossValue = shares * currentValue;
    const grossProfit = grossValue - investment.amount;
    const grossProfitPercent = (grossProfit / investment.amount) * 100;
    
    const netValue = grossValue * 0.67; // 33% tax
    const netProfit = netValue - investment.amount;
    const netProfitPercent = (netProfit / investment.amount) * 100;
    
    return {
      grossValue,
      netValue,
      grossProfit,
      netProfit,
      grossProfitPercent,
      netProfitPercent
    };
  };

  const handleSell = (investmentId: string) => {
    const investment = investments.find(inv => inv.id === investmentId);
    if (!investment || investment.sold) return;

    const updates = {
      sold: true,
      soldDate: new Date().toISOString().split('T')[0],
      soldFundValue: investment.currentFundValue
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

  const totalPortfolio = investments.reduce((total, investment) => {
    const calc = calculateInvestment(investment);
    return total + calc.netValue;
  }, 0);

  const totalInvested = investments.reduce((total, investment) => total + investment.amount, 0);
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
            <div className="text-2xl font-bold">${totalInvested.toLocaleString()}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Portfolio Value (Net)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">${totalPortfolio.toLocaleString()}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Net Profit</CardTitle>
          </CardHeader>
          <CardContent>
            <div className={`text-2xl font-bold ${totalNetProfit >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
              {totalNetProfit >= 0 ? '+' : ''}${totalNetProfit.toLocaleString()}
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
          ) : investments.length === 0 ? (
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
                    <th className="border border-border p-3 text-right">Amount</th>
                    <th className="border border-border p-3 text-right">Date</th>
                    <th className="border border-border p-3 text-right">Gross Value</th>
                    <th className="border border-border p-3 text-right">Net Value</th>
                    <th className="border border-border p-3 text-right">Net Profit</th>
                    <th className="border border-border p-3 text-center">Status</th>
                    <th className="border border-border p-3 text-center">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {investments.map((investment) => {
                    const calc = calculateInvestment(investment);
                    const isPositive = calc.netProfit >= 0;
                    
                    return (
                      <tr key={investment.id}>
                        <td className="border border-border p-3">{investment.fundName}</td>
                        <td className="border border-border p-3 text-right">
                          ${investment.amount.toLocaleString()}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {investment.investmentDate}
                        </td>
                        <td className="border border-border p-3 text-right">
                          ${calc.grossValue.toLocaleString()}
                        </td>
                        <td className="border border-border p-3 text-right">
                          ${calc.netValue.toLocaleString()}
                        </td>
                        <td className={`border border-border p-3 text-right font-semibold ${
                          isPositive ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                        }`}>
                          {isPositive ? '+' : ''}${calc.netProfit.toLocaleString()}
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
                          {!investment.sold && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleSell(investment.id)}
                            >
                              <TrendingDown className="mr-1 h-3 w-3" />
                              Sell
                            </Button>
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