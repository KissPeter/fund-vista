import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { InvestmentChart, type ReturnAnalysisRow } from "@/components/InvestmentChart";
import type { ChartData, Fund } from "@/services/investmentApi";

interface ChartPanelProps {
  chartData: ChartData | null;
  chartLoading: boolean;
  selectedFund: Fund | null;
  returnAnalysisRows: ReturnAnalysisRow[];
  selectedRange: number;
  onRangeChange: (months: number) => void;
}

/**
 * Analysis tab: chart + fund details (REF-002 Phase 5).
 *
 * Pure move of Index's analysis tab content; presentational only.
 */
export function ChartPanel({
  chartData,
  chartLoading,
  selectedFund,
  returnAnalysisRows,
  selectedRange,
  onRangeChange,
}: ChartPanelProps) {
  return (
    <div className="grid grid-cols-1 gap-6">
      <InvestmentChart
        data={chartData}
        loading={chartLoading}
        selectedFund={selectedFund}
        returnAnalysisRows={returnAnalysisRows}
        selectedRangeMonths={selectedRange}
        onRangeChange={onRangeChange}
      />

      {selectedFund && (
        <Card>
          <CardHeader>
            <CardTitle>Fund Details</CardTitle>
            <CardDescription>{selectedFund.portfolioName}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              <div className="space-y-2">
                <h4 className="font-medium">Basic Info</h4>
                <div className="text-sm space-y-1">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Fund No:</span>
                    <span className="font-mono">{selectedFund.fundNo}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Currency:</span>
                    <span>{selectedFund.currencyType}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Type:</span>
                    <span>{selectedFund.investmentType}</span>
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <h4 className="font-medium">Investment Options</h4>
                <div className="flex flex-wrap gap-1">
                  {selectedFund.regularityTypes.map((type) => (
                    <Badge key={type} variant="outline">
                      {type === 'ONETIME' ? 'One-time' : 'Regular'}
                    </Badge>
                  ))}
                </div>
              </div>

              <div className="space-y-2">
                <h4 className="font-medium">Characteristics</h4>
                <div className="space-y-1">
                  <Badge variant="outline">
                    {selectedFund.deviceClassType.replace('_', ' ')}
                  </Badge>
                  <br />
                  <Badge
                    variant="outline"
                    className={selectedFund.sustainabilityType === 'RESPONSIBLE_FUTURE'
                      ? 'bg-green-100 text-green-800 border-green-300'
                      : ''}
                  >
                    {selectedFund.sustainabilityType === 'RESPONSIBLE_FUTURE' ? 'ESG Focused' : 'Traditional'}
                  </Badge>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
