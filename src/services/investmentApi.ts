const BASE_URL = import.meta.env.VITE_INVESTMENT_API_BASE_URL || 'https://www.kh.hu';

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

export const investmentApi = {
  async getFunds(): Promise<Fund[]> {
    const response = await fetch(`${BASE_URL}/megtakaritas-befektetes/befektetes-kalkulator?p_p_id=yield_detailed_calculator&p_p_lifecycle=2&p_p_state=pop_up&p_p_mode=view&p_p_resource_id=cmdInvestmentFundsSearch&p_p_cacheability=cacheLevelPage`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*',
      },
      body: '_yield_detailed_calculator_sustainabilityType=&_yield_detailed_calculator_currencyType=&_yield_detailed_calculator_deviceClassType=&_yield_detailed_calculator_riskClassificationType='
    });

    if (!response.ok) {
      throw new Error('Failed to fetch funds');
    }

    const data = await response.json();
    return data.map((item: string) => JSON.parse(item));
  },

  async getCalculationData(
    fundId: number,
    amount: number = 50000,
    months: number = 12,
    regularityType: 'ONETIME' | 'REGULAR' = 'ONETIME'
  ): Promise<ChartData> {
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

    return response.json();
  },

  // Simple yield calculation based on historical data
  async getSimpleYield(primaryKey: number, months: number): Promise<string | null> {
    try {
      const data = await this.getCalculationData(primaryKey, 50000, months, 'ONETIME');
      
      if (data.diagram?.series?.[0]?.values && data.diagram.series[0].values.length > 1) {
        const values = data.diagram.series[0].values;
        const startValue = values[0];
        const endValue = values[values.length - 1];
        
        if (startValue !== undefined && endValue !== undefined) {
          // Simple percentage calculation: ((end - start) / start) * 100
          const percentChange = ((endValue - startValue) / Math.abs(startValue)) * 100;
          return `${percentChange.toFixed(2)}%`;
        }
      }
      
      // Fallback to the API's calculated yield if chart data isn't available
      const result = Object.values(data.calculationResults)[0] as CalculationResult;
      return result?.yieldPercent || null;
    } catch (error) {
      console.warn(`Failed to calculate simple yield for fund ${primaryKey}:`, error);
      return null;
    }
  }
};