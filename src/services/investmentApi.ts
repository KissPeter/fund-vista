const BASE_URL = import.meta.env.VITE_INVESTMENT_API_BASE_URL || '/api';
const ERSTE_BASE_URL = import.meta.env.VITE_ERSTE_API_BASE_URL || '/ersteapi';
const CALC_CACHE_TTL_MS = 30 * 60 * 1000;
const ERSTE_CACHE_TTL_MS = 12 * 60 * 60 * 1000;

export interface Fund {
  primaryKey: number;
  portfolioName: string;
  fundNo: string;
  localeLanguage: string;
  investmentType: string;
  regularityTypes: string[];
  currencyType: string;
  dateFrom: number;
  dateTo: number;
  deviceClassType: string;
  riskClassificationType: string;
  sustainabilityType: string;
}

export interface CalculationResult {
  startRate: string;
  sumInvestment: string;
  annualizedYieldPercent: string;
  dateTo: number;
  endRate: string;
  yieldValue: string;
  regularityType: string;
  yieldPercent: string;
  dateFrom: number;
}

export interface ChartData {
  diagram: {
    'scale-x': {
      labels: string[];
    };
    series: Array<{
      text: string;
      values: number[];
      'line-color': string;
    }>;
  };
  tableData: {
    results: Array<{
      primaryKey: number;
      portfolioName: string;
      yield: string;
      fundType: string;
      currency: string;
      riskClassification: string;
      nav: string;
    }>;
  };
  calculationResults: Record<string, CalculationResult>;
}

export interface ProviderFundsData {
  funds: Fund[];
  yields: Record<number, string>;
}

const getErsteYieldColumnIndex = (months: number) => {
  if (months <= 3) return 5; // 3 hó
  return 6; // 1 év
};

const mapErsteCategoryToDeviceClass = (category: string) => {
  const normalized = category.toLowerCase();
  if (normalized.includes("részvény")) return "STOCK_MATERIAL";
  if (normalized.includes("vegyes")) return "MIXED";
  if (normalized.includes("nyersanyag")) return "STOCK_MATERIAL";
  if (normalized.includes("abszolút")) return "MIXED";
  return "MIXED";
};

export const investmentApi = {
  async getFunds(): Promise<Fund[]> {
    const response = await fetch(
      `${BASE_URL}/megtakaritas-befektetes/befektetes-kalkulator?p_p_id=yield_detailed_calculator&p_p_lifecycle=2&p_p_state=pop_up&p_p_mode=view&p_p_resource_id=cmdInvestmentFundsSearch&p_p_cacheability=cacheLevelPage`
    );

    if (!response.ok) {
      throw new Error('Failed to fetch funds');
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error(`Unexpected funds response content-type: ${contentType || "unknown"}`);
    }

    const data = await response.json();
    return data.map((item: string | Fund) => typeof item === "string" ? JSON.parse(item) : item);
  },

  async getCalculationData(
    fundId: number,
    amount: number = 50000,
    months: number = 12,
    regularityType: 'ONETIME' | 'REGULAR' = 'ONETIME'
  ): Promise<ChartData> {
    const cacheKey = `calc_${fundId}_${amount}_${months}_${regularityType}`;
    const cached = localStorage.getItem(cacheKey);
    const cacheNow = Date.now();

    if (cached) {
      const parsed = JSON.parse(cached) as { timestamp: number; data: ChartData };
      // ponytail: short TTL to cut rate-limit risk; raise only if KH data is stable enough.
      if (cacheNow - parsed.timestamp < CALC_CACHE_TTL_MS) {
        return parsed.data;
      }
      localStorage.removeItem(cacheKey);
    }

    const now = Date.now();
    const dateFrom = now - (months * 30 * 24 * 60 * 60 * 1000); // Approximate months to milliseconds
    const dateTo = now;

    const response = await fetch(`${BASE_URL}/megtakaritas-befektetes/befektetes-kalkulator?p_p_id=yield_detailed_calculator&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view&p_p_resource_id=cmdGetCalculationResultsAndChartData&p_p_cacheability=cacheLevelPage`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*',
      },
      body: `_yield_detailed_calculator_intoThisFundcard-1=${fundId}&_yield_detailed_calculator_dateFromcard-1=${dateFrom}&_yield_detailed_calculator_dateTocard-1=${dateTo}&_yield_detailed_calculator_wouldHaveInvestedRegularitycard-1=${regularityType}&_yield_detailed_calculator_wouldHaveInvestedAmountcard-1=${amount}&_yield_detailed_calculator_cardIds=card-1`
    });

    if (!response.ok) {
      throw new Error('Failed to fetch calculation data');
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error(`Unexpected calculation response content-type: ${contentType || "unknown"}`);
    }

    const data = await response.json();
    localStorage.setItem(cacheKey, JSON.stringify({
      timestamp: cacheNow,
      data,
    }));
    return data;
  },

  async getErsteFunds(months: number): Promise<ProviderFundsData> {
    const cacheKey = `erste_funds_${months}m`;
    const cached = localStorage.getItem(cacheKey);
    const now = Date.now();

    if (cached) {
      const parsed = JSON.parse(cached) as { timestamp: number; data: ProviderFundsData };
      if (now - parsed.timestamp < ERSTE_CACHE_TTL_MS) {
        return parsed.data;
      }
      localStorage.removeItem(cacheKey);
    }

    const searchParams = "parameters%5B%5D=man_2&parameters%5B%5D=man_52&ret_min=13&ret_max=160";
    const refererUrl = `${ERSTE_BASE_URL}/befektetesi_alapok/kereses?${searchParams}`;
    const listUrl = `${ERSTE_BASE_URL}/funds/search_results/list?layout=website`;
    const requestBody = "parameters%5B%5D=man_2&parameters%5B%5D=man_52&keyword=&toggles%5Bstarred%5D=1&ret_min=13&ret_max=160";

    await fetch(refererUrl);

    const response = await fetch(listUrl, {
      method: "POST",
      headers: {
        "Accept": "text/html, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: requestBody,
    });

    if (!response.ok) {
      throw new Error("Failed to fetch Erste funds");
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/html")) {
      throw new Error(`Unexpected Erste response content-type: ${contentType || "unknown"}`);
    }

    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    const rows = Array.from(doc.querySelectorAll("tr.fundClick"));
    const yieldColumn = getErsteYieldColumnIndex(months);
    const funds: Fund[] = [];
    const yields: Record<number, string> = {};

    rows.forEach((row) => {
      const cells = row.querySelectorAll("td");
      if (cells.length < 7) return;

      const anchor = cells[0]?.querySelector("a");
      const id = Number(row.getAttribute("id"));
      if (!anchor || !Number.isFinite(id)) return;

      const name = anchor.textContent?.trim() || "";
      const fundNo = anchor.getAttribute("href")?.split("/").pop() || `${id}`;
      const currency = cells[2]?.textContent?.trim() || "N/A";
      const category = cells[3]?.textContent?.trim() || "Unknown";
      const yieldText = cells[yieldColumn]?.textContent?.trim() || "";

      funds.push({
        primaryKey: id,
        portfolioName: name,
        fundNo,
        localeLanguage: "hu",
        investmentType: "ERSTE",
        regularityTypes: ["ONETIME"],
        currencyType: currency,
        dateFrom: 0,
        dateTo: 0,
        deviceClassType: mapErsteCategoryToDeviceClass(category),
        riskClassificationType: "",
        sustainabilityType: "TRADITIONAL",
      });

      if (yieldText && yieldText !== "-") {
        yields[id] = yieldText;
      }
    });

    const result = { funds, yields };
    localStorage.setItem(cacheKey, JSON.stringify({
      timestamp: now,
      data: result,
    }));
    return result;
  },

  // Simple yield calculation based on historical data
  async getSimpleYield(primaryKey: number, months: number): Promise<string | null> {
    try {
      console.log(`🌐 API: Fetching calculation data for fund ${primaryKey}, ${months} months`);
      const data = await this.getCalculationData(primaryKey, 50000, months, 'ONETIME');
      
      console.log(`📈 API: Chart data for fund ${primaryKey}:`, {
        hasChartData: !!data.diagram?.series?.[0]?.values,
        valuesLength: data.diagram?.series?.[0]?.values?.length || 0,
        firstValue: data.diagram?.series?.[0]?.values?.[0],
        lastValue: data.diagram?.series?.[0]?.values?.[data.diagram?.series?.[0]?.values?.length - 1],
        hasCalculationResults: !!Object.keys(data.calculationResults).length
      });
      
      if (data.diagram?.series?.[0]?.values && data.diagram.series[0].values.length > 1) {
        const values = data.diagram.series[0].values;
        const startValue = values[0];
        const endValue = values[values.length - 1];
        
        console.log(`🧮 API: Calculating percentage for fund ${primaryKey}:`, {
          startValue,
          endValue,
          valuesCount: values.length
        });
        
        if (startValue !== undefined && endValue !== undefined) {
          // Simple percentage calculation: ((end - start) / start) * 100
          const percentChange = ((endValue - startValue) / Math.abs(startValue)) * 100;
          const result = `${percentChange.toFixed(2)}%`;
          console.log(`✅ API: Calculated yield for fund ${primaryKey}: ${result}`);
          return result;
        }
      }
      
      // Fallback to the API's calculated yield if chart data isn't available
      const result = Object.values(data.calculationResults)[0] as CalculationResult;
      const fallbackYield = result?.yieldPercent || null;
      console.log(`⚠️ API: Using fallback yield for fund ${primaryKey}:`, {
        hasCalculationResult: !!result,
        yieldPercent: result?.yieldPercent,
        fallbackYield
      });
      return fallbackYield;
    } catch (error) {
      console.error(`💥 API: Failed to calculate simple yield for fund ${primaryKey}:`, error);
      return null;
    }
  }
};