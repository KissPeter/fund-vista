import type { Fund } from "@/services/investmentApi";

export const getFundExternalLink = (fund?: Fund | null) => {
  if (!fund?.fundNo) return null;
  const fundNo = fund.fundNo.toUpperCase();
  const currency = fund.currencyType?.toUpperCase();

  if (fundNo.startsWith("HU")) {
    return `https://www.bamosz.hu/alapoldal?isin=${fundNo}`;
  }

  if (fundNo.startsWith("BE") && (currency === "EUR" || currency === "USD")) {
    return `https://markets.ft.com/data/funds/tearsheet/summary?s=${fundNo}:${currency}`;
  }

  return null;
};
