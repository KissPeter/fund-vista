import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { CalendarIcon, TrendingUp } from "lucide-react";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import type { ChartData, Fund } from "@/services/investmentApi";
import type { Investment } from "@/types/investment";
import { investmentStorage } from "@/services/investmentStorage";
import { exchangeRateApi } from "@/services/exchangeRateApi";
import { useToast } from "@/hooks/use-toast";

interface InvestModalProps {
  fund: Fund;
  currentFundValue: number;
  chartData?: ChartData | null;
}

const parseKhDate = (value: string): Date | null => {
  const parts = value.split(".").filter(Boolean);
  if (parts.length !== 3) return null;
  const [a, b, c] = parts.map(Number);
  const isYearFirst = parts[0].length === 4;
  const year = isYearFirst ? a : c;
  const month = b;
  const day = isYearFirst ? c : a;
  if (!day || !month || !year) return null;
  return new Date(year, month - 1, day);
};

export const InvestModal = ({ fund, currentFundValue, chartData }: InvestModalProps) => {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState<Date>();
  const [fundValueAtDate, setFundValueAtDate] = useState(currentFundValue.toString());
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    if (!date) {
      setFundValueAtDate(currentFundValue.toString());
      return;
    }

    const labels = chartData?.diagram?.["scale-x"]?.labels ?? [];
    const values = chartData?.diagram?.series?.[0]?.values ?? [];
    if (labels.length === 0 || values.length === 0 || labels.length !== values.length) {
      setFundValueAtDate(currentFundValue.toString());
      return;
    }

    let bestIndex = values.length - 1;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let i = 0; i < labels.length; i++) {
      const d = parseKhDate(labels[i]);
      if (!d) continue;
      const distance = Math.abs(d.getTime() - date.getTime());
      if (distance < bestDistance) {
        bestDistance = distance;
        bestIndex = i;
      }
    }

    setFundValueAtDate((values[bestIndex] ?? currentFundValue).toString());
  }, [date, chartData, currentFundValue]);

  const handleInvest = async () => {
    if (!amount || !date || !fundValueAtDate) {
      toast({
        title: "Error",
        description: "Please fill in all fields",
        variant: "destructive",
      });
      return;
    }

    const amountValue = parseFloat(amount);
    const fundValue = parseFloat(fundValueAtDate);
    if (!Number.isFinite(amountValue) || amountValue <= 0 || !Number.isFinite(fundValue) || fundValue <= 0) {
      toast({
        title: "Error",
        description: "Invalid investment amount or fund value",
        variant: "destructive",
      });
      return;
    }

    setSaving(true);
    try {
      const investmentDate = format(date, "yyyy-MM-dd");
      const fundCurrency = (fund.currencyType || "HUF").toUpperCase();
      const fxRateToHufAtInvestment = await exchangeRateApi.getRateToHuf(fundCurrency, investmentDate);
      const currentFxRateToHuf = await exchangeRateApi.getRateToHuf(fundCurrency);
      const amountInFundCurrency = amountValue;
      const amountInHuf = amountInFundCurrency * fxRateToHufAtInvestment;

      const investment: Investment = {
        id: `inv_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        fundId: fund.primaryKey,
        fundName: fund.portfolioName,
        amount: amountInHuf,
        fundCurrency,
        amountInFundCurrency,
        fxRateToHufAtInvestment,
        currentFxRateToHuf,
        investmentDate,
        fundValueAtInvestment: fundValue,
        currentFundValue,
        sold: false,
      };

      investmentStorage.saveInvestment(investment);
      
      toast({
        title: "Investment Added",
        description: `Added investment of ${amount} ${fundCurrency} in ${fund.portfolioName}`,
      });

      // Reset form
      setAmount("");
      setDate(undefined);
      setFundValueAtDate("");
      setOpen(false);
    } catch (error) {
      toast({
        title: "FX error",
        description: "Could not fetch exchange rate for this investment date.",
        variant: "destructive",
      });
      console.error("Failed to save investment with FX conversion:", error);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="w-full" size="lg">
          <TrendingUp className="mr-2 h-4 w-4" />
          Invest
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add Investment</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="fund">Fund</Label>
            <Input id="fund" value={fund.portfolioName} disabled />
          </div>
          
          <div className="space-y-2">
            <Label htmlFor="amount">Investment Amount ({(fund.currencyType || "HUF").toUpperCase()})</Label>
            <Input
              id="amount"
              type="number"
              placeholder="10000"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label>Investment Date</Label>
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className={cn(
                    "w-full justify-start text-left font-normal",
                    !date && "text-muted-foreground"
                  )}
                >
                  <CalendarIcon className="mr-2 h-4 w-4" />
                  {date ? format(date, "PPP") : "Pick a date"}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-0">
                <Calendar
                  mode="single"
                  selected={date}
                  onSelect={setDate}
                  disabled={(date) => date > new Date()}
                  initialFocus
                />
              </PopoverContent>
            </Popover>
          </div>

          <div className="space-y-2">
            <Label htmlFor="fundValue">Fund Value at Investment Date (auto)</Label>
            <Input id="fundValue" type="number" value={fundValueAtDate} disabled />
          </div>

          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setOpen(false)} className="flex-1">
              Cancel
            </Button>
            <Button onClick={handleInvest} className="flex-1" disabled={saving}>
              {saving ? "Adding..." : "Add Investment"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};