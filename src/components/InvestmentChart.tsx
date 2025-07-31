import { Line, LineChart, ResponsiveContainer, XAxis, YAxis, Tooltip, Legend } from "recharts";
import { ChartContainer, ChartTooltipContent } from "@/components/ui/chart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ChartData } from "@/services/investmentApi";

interface InvestmentChartProps {
  data: ChartData | null;
  loading: boolean;
}

export const InvestmentChart = ({ data, loading }: InvestmentChartProps) => {
  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Loading Chart...</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[400px] flex items-center justify-center">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Select a Fund</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[400px] flex items-center justify-center text-muted-foreground">
            Click on a fund card to view its performance chart
          </div>
        </CardContent>
      </Card>
    );
  }

  // Transform chart data to show actual values
  const transformChartData = () => {
    if (!data?.diagram?.series?.[0]?.values) return [];
    
    return data.diagram['scale-x'].labels.map((label, index) => {
      const item: any = { date: label };
      data.diagram.series.forEach((series, seriesIndex) => {
        const values = series.values;
        const currentValue = values[index] || 0;
        
        // Use actual values instead of percentage changes
        item[`series${seriesIndex}`] = currentValue;
      });
      return item;
    });
  };

  const chartData = transformChartData();

  const chartConfig = data.diagram.series.reduce((config, series, index) => {
    config[`series${index}`] = {
      label: series.text,
      color: series['line-color'],
    };
    return config;
  }, {} as any);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Performance Chart</CardTitle>
        {data.tableData.results.length > 0 && (
          <div className="text-sm text-muted-foreground">
            {data.tableData.results[0].portfolioName}
          </div>
        )}
      </CardHeader>
      <CardContent>
        <ChartContainer config={chartConfig} className="h-[400px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <XAxis 
                dataKey="date" 
                tick={{ fontSize: 12 }}
                tickFormatter={(value) => {
                  // Format date to show only month/day
                  if (typeof value === 'string') {
                    const parts = value.split('.');
                    if (parts.length >= 2) {
                      return `${parts[1]}/${parts[2]}`;
                    }
                  }
                  return value;
                }}
              />
              <YAxis 
                tick={{ fontSize: 12 }}
                tickFormatter={(value) => value.toLocaleString()}
              />
              <Tooltip content={<ChartTooltipContent />} />
              <Legend />
              {data.diagram.series.map((series, index) => (
                <Line
                  key={index}
                  type="monotone"
                  dataKey={`series${index}`}
                  stroke={series['line-color']}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  activeDot={{ r: 5 }}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </ChartContainer>
        
        {/* Returns Analysis Table */}
        <div className="mt-6">
          <h3 className="text-lg font-semibold mb-4">Returns Analysis</h3>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse border border-border">
              <thead>
                <tr className="bg-muted/50">
                  <th className="border border-border p-3 text-left">Period</th>
                  <th className="border border-border p-3 text-right">Return %</th>
                  <th className="border border-border p-3 text-right">Start Value</th>
                  <th className="border border-border p-3 text-right">End Value</th>
                </tr>
              </thead>
              <tbody>
                {(() => {
                  const periods = [
                    { label: '1 Month', months: 1 },
                    { label: '3 Months', months: 3 },
                    { label: '6 Months', months: 6 },
                    { label: '12 Months', months: 12 }
                  ];

                  return periods.map(({ label, months }) => {
                    const series = data.diagram.series[0]; // Use first series
                    if (!series?.values || series.values.length === 0) {
                      return (
                        <tr key={label}>
                          <td className="border border-border p-3">{label}</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                        </tr>
                      );
                    }

                    const values = series.values;
                    const startValue = values[0];
                    const endValue = values[values.length - 1];
                    
                    if (startValue === 0 || !startValue || !endValue) {
                      return (
                        <tr key={label}>
                          <td className="border border-border p-3">{label}</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                          <td className="border border-border p-3 text-right text-muted-foreground">-</td>
                        </tr>
                      );
                    }

                    // Use same calculation as API: Math.abs(startValue) as denominator
                    const returnPercent = ((endValue - startValue) / Math.abs(startValue)) * 100;
                    const isPositive = returnPercent >= 0;

                    return (
                      <tr key={label}>
                        <td className="border border-border p-3">{label}</td>
                        <td className={`border border-border p-3 text-right font-semibold ${
                          isPositive ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                        }`}>
                          {isPositive ? '+' : ''}{returnPercent.toFixed(2)}%
                        </td>
                        <td className="border border-border p-3 text-right">
                          {startValue.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                        </td>
                        <td className="border border-border p-3 text-right">
                          {endValue.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                        </td>
                      </tr>
                    );
                  });
                })()}
              </tbody>
            </table>
          </div>
        </div>

        {data.calculationResults && Object.keys(data.calculationResults).length > 0 && (
          <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-4">
            {Object.entries(data.calculationResults).map(([key, result]) => (
              <div key={key} className="text-center p-3 bg-muted/50 rounded-lg">
                <div className="text-2xl font-bold text-primary">
                  {result.yieldPercent}
                </div>
                <div className="text-sm text-muted-foreground">Total Yield</div>
                <div className="text-xs text-muted-foreground mt-1">
                  {result.regularityType === 'ONETIME' ? 'One-time' : 'Regular'} Investment
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
};