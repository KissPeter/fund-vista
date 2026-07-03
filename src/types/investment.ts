export interface Investment {
  id: string;
  fundId: number;
  fundName: string;
  amount: number;
  fundCurrency?: string;
  amountInFundCurrency?: number;
  fxRateToHufAtInvestment?: number;
  currentFxRateToHuf?: number;
  investmentDate: string;
  fundValueAtInvestment: number;
  currentFundValue?: number;
  sold: boolean;
  soldDate?: string;
  soldFundValue?: number;
  soldFxRateToHuf?: number;
}

export interface InvestmentCalculation {
  grossValue: number;
  netValue: number;
  grossProfit: number;
  netProfit: number;
  grossProfitPercent: number;
  netProfitPercent: number;
}