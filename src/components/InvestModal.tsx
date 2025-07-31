import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { CalendarIcon, TrendingUp } from "lucide-react";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import type { Fund } from "@/services/investmentApi";
import type { Investment } from "@/types/investment";
import { investmentStorage } from "@/services/investmentStorage";
import { useToast } from "@/hooks/use-toast";

interface InvestModalProps {
  fund: Fund;
  currentFundValue: number;
}

export const InvestModal = ({ fund, currentFundValue }: InvestModalProps) => {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState<Date>();
  const [fundValueAtDate, setFundValueAtDate] = useState("");
  const { toast } = useToast();

  const handleInvest = () => {
    if (!amount || !date || !fundValueAtDate) {
      toast({
        title: "Error",
        description: "Please fill in all fields",
        variant: "destructive",
      });
      return;
    }

    const investment: Investment = {
      id: `inv_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
      fundId: fund.primaryKey,
      fundName: fund.portfolioName,
      amount: parseFloat(amount),
      investmentDate: format(date, "yyyy-MM-dd"),
      fundValueAtInvestment: parseFloat(fundValueAtDate),
      currentFundValue,
      sold: false,
    };

    investmentStorage.saveInvestment(investment);
    
    toast({
      title: "Investment Added",
      description: `Added investment of $${amount} in ${fund.portfolioName}`,
    });

    // Reset form
    setAmount("");
    setDate(undefined);
    setFundValueAtDate("");
    setOpen(false);
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
            <Label htmlFor="amount">Investment Amount ($)</Label>
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
            <Label htmlFor="fundValue">Fund Value at Investment Date</Label>
            <Input
              id="fundValue"
              type="number"
              placeholder="150.00"
              value={fundValueAtDate}
              onChange={(e) => setFundValueAtDate(e.target.value)}
            />
          </div>

          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setOpen(false)} className="flex-1">
              Cancel
            </Button>
            <Button onClick={handleInvest} className="flex-1">
              Add Investment
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};