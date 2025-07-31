export interface Investment {
  id: string;
  fundId: number;
  fundName: string;
  amount: number;
  investmentDate: string;
  fundValueAtInvestment: number;
  currentFundValue?: number;
  sold: boolean;
  soldDate?: string;
  soldFundValue?: number;
}

export interface InvestmentCalculation {
  grossValue: number;
  netValue: number;
  grossProfit: number;
  netProfit: number;
  grossProfitPercent: number;
  netProfitPercent: number;
}