import type { Investment, InvestmentCalculation } from "@/types/investment";

/** A number that can take part in arithmetic (no NaN, no Infinity). */
export const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

const positiveOrNull = (value: unknown): number | null =>
  isFiniteNumber(value) && value > 0 ? value : null;

/**
 * The current unit value of the fund for this investment.
 * For a sold investment the value is frozen at the sell price, otherwise it
 * is the last fetched (and possibly missing) current value.
 */
export const getCurrentFundValue = (investment: Investment): number | null => {
  const value = investment.sold ? investment.soldFundValue : investment.currentFundValue;
  return isFiniteNumber(value) ? value : null;
};

/**
 * Calculate the money figures of one investment.
 *
 * Returns `null` when the valuation is not available (the current fund
 * value could not be fetched, or the purchase value is missing/zero). The
 * caller must treat `null` as "value unavailable" and must never render a
 * number derived from it - that is how a missing valuation used to turn
 * into `NaN HUF` in the table and in the portfolio totals.
 */
export const calculateInvestment = (
  investment: Investment
): InvestmentCalculation | null => {
  const currentValue = getCurrentFundValue(investment);
  if (currentValue === null) return null;

  const purchaseValue = positiveOrNull(investment.fundValueAtInvestment);
  if (purchaseValue === null) return null;

  const amount = investment.amount;
  if (!isFiniteNumber(amount)) return null;

  const fxAtInvestment = positiveOrNull(investment.fxRateToHufAtInvestment) ?? 1;

  const amountInFundCurrency =
    isFiniteNumber(investment.amountInFundCurrency) && investment.amountInFundCurrency !== 0
      ? investment.amountInFundCurrency
      : amount / fxAtInvestment;

  const currentFx = investment.sold
    ? (positiveOrNull(investment.soldFxRateToHuf) ??
      positiveOrNull(investment.currentFxRateToHuf) ??
      1)
    : (positiveOrNull(investment.currentFxRateToHuf) ?? 1);

  const shares = amountInFundCurrency / purchaseValue;
  const grossValue = shares * currentValue;
  const grossValueHuf = grossValue * currentFx;
  const grossProfit = grossValueHuf - amount;

  const calculation: InvestmentCalculation = {
    grossValue: grossValueHuf,
    netValue: grossValueHuf,
    grossProfit,
    netProfit: grossProfit,
    grossProfitPercent: amount !== 0 ? (grossProfit / amount) * 100 : 0,
    netProfitPercent: amount !== 0 ? (grossProfit / amount) * 100 : 0,
  };

  // Belt and braces: never hand a NaN/Infinity to the UI.
  if (!Object.values(calculation).every(isFiniteNumber)) return null;

  return calculation;
};

export interface PortfolioTotals {
  /** The sum of the invested amounts of the priced investments. */
  invested: number;
  /** The sum of the current values of the priced investments. */
  value: number;
  /** value - invested */
  profit: number;
  /** profit / invested in percent, 0 when nothing is priced. */
  profitPercent: number;
  /** How many investments took part in the totals. */
  pricedCount: number;
  /** How many investments were skipped (no valuation). */
  unavailableCount: number;
}

/**
 * The portfolio totals over the priced investments only. An investment
 * without a valuation is skipped (it is neither valued nor counted as
 * invested), so the totals stay meaningful and never contain NaN.
 */
export const calculatePortfolioTotals = (investments: Investment[]): PortfolioTotals => {
  let invested = 0;
  let value = 0;
  let pricedCount = 0;
  let unavailableCount = 0;

  for (const investment of investments) {
    const calculation = calculateInvestment(investment);
    if (!calculation) {
      unavailableCount += 1;
      continue;
    }
    invested += investment.amount;
    value += calculation.netValue;
    pricedCount += 1;
  }

  const profit = value - invested;

  return {
    invested,
    value,
    profit,
    profitPercent: invested > 0 ? (profit / invested) * 100 : 0,
    pricedCount,
    unavailableCount,
  };
};
