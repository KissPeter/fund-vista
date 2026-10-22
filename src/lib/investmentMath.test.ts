import test from "node:test";
import assert from "node:assert/strict";

import {
  calculateInvestment,
  calculatePortfolioTotals,
  getCurrentFundValue,
} from "./investmentMath.ts";
import type { Investment } from "../types/investment.ts";

const baseInvestment = (overrides: Partial<Investment> = {}): Investment => ({
  id: "inv-1",
  fundId: 42,
  fundName: "Test Fund",
  amount: 100000,
  fundCurrency: "HUF",
  amountInFundCurrency: 100000,
  fxRateToHufAtInvestment: 1,
  currentFxRateToHuf: 1,
  investmentDate: "2026-01-15",
  fundValueAtInvestment: 100,
  currentFundValue: 110,
  sold: false,
  ...overrides,
});

test("a priced investment is calculated", () => {
  const calc = calculateInvestment(baseInvestment());
  assert.ok(calc);
  assert.equal(calc.grossValue, 110000);
  assert.equal(calc.grossProfit, 10000);
  assert.equal(calc.grossProfitPercent, 10);
});

test("a missing current fund value means the valuation is unavailable, not NaN", () => {
  const calc = calculateInvestment(baseInvestment({ currentFundValue: undefined }));
  assert.equal(calc, null);
});

test("a NaN current fund value means the valuation is unavailable", () => {
  const calc = calculateInvestment(baseInvestment({ currentFundValue: Number.NaN }));
  assert.equal(calc, null);
});

test("a missing purchase value means the valuation is unavailable", () => {
  const calc = calculateInvestment(
    baseInvestment({ fundValueAtInvestment: 0 })
  );
  assert.equal(calc, null);
});

test("a sold investment uses the sold fund value", () => {
  const calc = calculateInvestment(
    baseInvestment({ sold: true, soldFundValue: 120 })
  );
  assert.ok(calc);
  assert.equal(calc.grossValue, 120000);
  assert.equal(getCurrentFundValue(baseInvestment({ sold: true, soldFundValue: 120 })), 120);
});

test("a sold investment without a sold value is unavailable", () => {
  const calc = calculateInvestment(
    baseInvestment({ sold: true, soldFundValue: undefined })
  );
  assert.equal(calc, null);
});

test("the totals skip the unpriced investments and stay finite", () => {
  const totals = calculatePortfolioTotals([
    baseInvestment(),
    baseInvestment({ id: "inv-2", currentFundValue: undefined }),
    baseInvestment({ id: "inv-3", currentFundValue: 90 }),
  ]);

  assert.equal(totals.pricedCount, 2);
  assert.equal(totals.unavailableCount, 1);
  assert.equal(totals.invested, 200000);
  assert.equal(totals.value, 110000 + 90000);
  assert.equal(totals.profit, 0);
  assert.ok(Number.isFinite(totals.profitPercent));
});

test("the totals of an empty portfolio are zero, not NaN", () => {
  const totals = calculatePortfolioTotals([]);
  assert.equal(totals.value, 0);
  assert.equal(totals.invested, 0);
  assert.equal(totals.profit, 0);
  assert.equal(totals.profitPercent, 0);
});

test("the totals of only unpriced investments are zero, not NaN", () => {
  const totals = calculatePortfolioTotals([
    baseInvestment({ currentFundValue: undefined }),
  ]);
  assert.equal(totals.value, 0);
  assert.equal(totals.invested, 0);
  assert.equal(totals.profitPercent, 0);
  assert.equal(totals.unavailableCount, 1);
});

test("no rendered figure is ever NaN", () => {
  const investments = [
    baseInvestment(),
    baseInvestment({ id: "inv-2", currentFundValue: undefined }),
    baseInvestment({ id: "inv-3", fundValueAtInvestment: 0 }),
  ];

  const totals = calculatePortfolioTotals(investments);
  const figures = [
    totals.value,
    totals.invested,
    totals.profit,
    totals.profitPercent,
  ];
  for (const investment of investments) {
    const calc = calculateInvestment(investment);
    if (calc) figures.push(...Object.values(calc));
  }

  for (const figure of figures) {
    assert.ok(Number.isFinite(figure), `expected a finite figure, got ${figure}`);
  }
});
