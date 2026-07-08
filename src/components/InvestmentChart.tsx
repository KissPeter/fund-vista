import { useEffect, useRef, useState } from "react";
import { Brush, CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis, Tooltip, Legend } from "recharts";
import { ChartContainer, ChartTooltipContent } from "@/components/ui/chart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InvestModal } from "@/components/InvestModal";
import { DateRangeFilter } from "@/components/DateRangeFilter";
import type { ChartData, Fund } from "@/services/investmentApi";
import { getFundExternalLink } from "@/lib/fundExternalLink";
import { ExternalLink } from "lucide-react";

export interface ReturnAnalysisRow {
  label: string;
  returnPercent: number | null;
  startValue: number | null;
  endValue: number | null;
}

interface InvestmentChartProps {
  data: ChartData | null;
  loading: boolean;
  selectedFund?: Fund;
  returnAnalysisRows?: ReturnAnalysisRow[];
  selectedRangeMonths?: number;
  onRangeChange?: (months: number) => void;
}

export const InvestmentChart = ({
  data,
  loading,
  selectedFund,
  returnAnalysisRows = [],
  selectedRangeMonths = 12,
  onRangeChange,
}: InvestmentChartProps) => {
  const [rebaseStartIndex, setRebaseStartIndex] = useState(0);
  const [committedBrushRange, setCommittedBrushRange] = useState<{ startIndex: number; endIndex: number }>({
    startIndex: 0,
    endIndex: 0,
  });
  const pendingBrushRangeRef = useRef<{ startIndex: number; endIndex: number } | null>(null);

  useEffect(() => {
    const maxIndex = Math.max(0, (data?.diagram?.series?.[0]?.values?.length || 1) - 1);
    setRebaseStartIndex(0);
    setCommittedBrushRange({ startIndex: 0, endIndex: maxIndex });
    pendingBrushRangeRef.current = null;
  }, [data, selectedRangeMonths]);

  useEffect(() => {
    const commitPendingRange = () => {
      const pending = pendingBrushRangeRef.current;
      if (pending) {
        setCommittedBrushRange(pending);
        setRebaseStartIndex(pending.startIndex);
        pendingBrushRangeRef.current = null;
      }
    };

    window.addEventListener("mouseup", commitPendingRange);
    window.addEventListener("touchend", commitPendingRange);
    return () => {
      window.removeEventListener("mouseup", commitPendingRange);
      window.removeEventListener("touchend", commitPendingRange);
    };
  }, []);

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
        const safeStartIndex = Math.max(0, Math.min(rebaseStartIndex, values.length - 1));
        const startValue = values[safeStartIndex] || values[0] || 0;
        
        // Use actual values instead of percentage changes
        item[`series${seriesIndex}`] = currentValue;
        item[`series${seriesIndex}Pct`] = startValue
          ? ((currentValue - startValue) / Math.abs(startValue)) * 100
          : 0;
      });
      return item;
    });
  };

  const chartData = transformChartData();
  const externalLink = getFundExternalLink(selectedFund);

  const chartConfig = data.diagram.series.reduce((config, series, index) => {
    config[`series${index}`] = {
      label: series.text,
      color: series['line-color'],
    };
    return config;
  }, {} as any);

  const zoomedChartData = chartData.slice(
    committedBrushRange.startIndex,
    committedBrushRange.endIndex + 1
  );

  const handleBrushChange = (range: { startIndex?: number; endIndex?: number }) => {
    if (typeof range.startIndex !== "number" && typeof range.endIndex !== "number") return;
    const nextStart = typeof range.startIndex === "number" ? range.startIndex : committedBrushRange.startIndex;
    const nextEnd = typeof range.endIndex === "number" ? range.endIndex : committedBrushRange.endIndex;
    const startIndex = Math.max(0, Math.min(nextStart, nextEnd));
    const endIndex = Math.max(startIndex, nextEnd);
    const currentPending = pendingBrushRangeRef.current ?? committedBrushRange;
    if (startIndex === currentPending.startIndex && endIndex === currentPending.endIndex) return;
    // ponytail: apply after drag release to avoid fighting brush interaction.
    pendingBrushRangeRef.current = { startIndex, endIndex };
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <CardTitle>Performance Chart</CardTitle>
          {onRangeChange && (
            <DateRangeFilter selectedRange={selectedRangeMonths} onRangeChange={onRangeChange} />
          )}
        </div>
        {data.tableData.results.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>{data.tableData.results[0].portfolioName}</span>
            {externalLink && (
              <a
                href={externalLink}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-6 w-6 items-center justify-center rounded-sm text-primary hover:bg-primary/10"
                title="Open external details"
                aria-label="Open external details"
              >
                <ExternalLink className="h-4 w-4" />
              </a>
            )}
          </div>
        )}
      </CardHeader>
      <CardContent>
        <ChartContainer config={chartConfig} className="h-[320px] w-full !aspect-auto">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={zoomedChartData}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
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
                  name={series.text}
                  stroke={series['line-color']}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  activeDot={{ r: 5 }}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </ChartContainer>

        <div className="mt-6">
          <h3 className="text-lg font-semibold mb-3">Performance (%)</h3>
          <ChartContainer config={chartConfig} className="h-[260px] w-full !aspect-auto">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData}>
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 12 }}
                  tickFormatter={(value) => {
                    if (typeof value === "string") {
                      const parts = value.split(".");
                      if (parts.length >= 2) {
                        return `${parts[1]}/${parts[2]}`;
                      }
                    }
                    return value;
                  }}
                />
                <YAxis
                  tick={{ fontSize: 12 }}
                  domain={[-20, "auto"]}
                  tickFormatter={(value) => `${value.toFixed(1)}%`}
                />
                <Tooltip content={<ChartTooltipContent />} />
                <Legend />
                {data.diagram.series.map((series, index) => (
                  <Line
                    key={`pct-${index}`}
                    type="monotone"
                    dataKey={`series${index}Pct`}
                    name={`${series.text} (%)`}
                    stroke={series["line-color"]}
                    strokeWidth={2}
                    dot={false}
                  />
                ))}
                <Brush
                  dataKey="date"
                  height={30}
                  stroke="#8884d8"
                  startIndex={committedBrushRange.startIndex}
                  endIndex={committedBrushRange.endIndex}
                  onChange={handleBrushChange}
                />
              </LineChart>
            </ResponsiveContainer>
          </ChartContainer>
        </div>
        
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
                {returnAnalysisRows.map((row) => {
                  const isEmpty = row.returnPercent === null || row.startValue === null || row.endValue === null;
                  const isPositive = (row.returnPercent ?? 0) >= 0;

                  return (
                    <tr key={row.label}>
                      <td className="border border-border p-3">{row.label}</td>
                      <td className={`border border-border p-3 text-right ${isEmpty ? "text-muted-foreground" : `font-semibold ${isPositive ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}`}>
                        {isEmpty ? "-" : `${isPositive ? "+" : ""}${row.returnPercent!.toFixed(2)}%`}
                      </td>
                      <td className="border border-border p-3 text-right">
                        {isEmpty ? "-" : row.startValue!.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </td>
                      <td className="border border-border p-3 text-right">
                        {isEmpty ? "-" : row.endValue!.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </td>
                    </tr>
                  );
                })}
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
        
        {/* Investment Button */}
        {selectedFund && data && (
          <div className="mt-6">
            <InvestModal 
              fund={selectedFund} 
              currentFundValue={data.diagram?.series?.[0]?.values?.slice(-1)[0] || 0}
              chartData={data}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
};