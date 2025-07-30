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