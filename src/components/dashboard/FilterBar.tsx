import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { DateRangeFilter } from "@/components/DateRangeFilter";
import type { Provider } from "@/hooks/useFunds";
import { Search } from "lucide-react";

interface FilterBarProps {
  searchTerm: string;
  onSearchChange: (value: string) => void;
  provider: Provider;
  onProviderChange: (provider: Provider) => void;
  selectedCurrency: string;
  onCurrencyChange: (currency: string) => void;
  currencyButtons: string[];
  selectedRange: number;
  onRangeChange: (months: number) => void;
  onFindTopGainer: () => void;
  actionDisabled: boolean;
  foundCount: number;
  loading: boolean;
  yieldsLoading: boolean;
  yieldProgress: number;
}

/**
 * Search & filter card with progress (REF-002 Phase 5).
 *
 * Pure move of Index's funds-tab control card; presentational only.
 */
export function FilterBar({
  searchTerm,
  onSearchChange,
  provider,
  onProviderChange,
  selectedCurrency,
  onCurrencyChange,
  currencyButtons,
  selectedRange,
  onRangeChange,
  onFindTopGainer,
  actionDisabled,
  foundCount,
  loading,
  yieldsLoading,
  yieldProgress,
}: FilterBarProps) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Search className="h-5 w-5" />
          Search & Filter
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-end gap-3 flex-wrap lg:flex-nowrap">
          <div className="flex flex-wrap items-end gap-3 flex-1 min-w-0">
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Search</div>
              <Input
                placeholder="Search fund by name or number"
                value={searchTerm}
                onChange={(e) => onSearchChange(e.target.value)}
                className="h-9 w-[220px] bg-background"
              />
            </div>

            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Provider</div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant={provider === "KH" ? "default" : "outline"}
                  size="sm"
                  onClick={() => onProviderChange("KH")}
                >
                  K&H
                </Button>
                <Button
                  variant={provider === "ERSTE" ? "default" : "outline"}
                  size="sm"
                  onClick={() => onProviderChange("ERSTE")}
                >
                  ERSTE
                </Button>
              </div>
            </div>

            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Currency</div>
              <div className="flex flex-wrap gap-2">
                {currencyButtons.map((currency) => (
                  <Button
                    key={currency}
                    variant={selectedCurrency === currency ? "default" : "outline"}
                    size="sm"
                    onClick={() => onCurrencyChange(currency)}
                    className="min-w-[56px]"
                  >
                    {currency}
                  </Button>
                ))}
              </div>
            </div>

            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Time</div>
              <DateRangeFilter
                selectedRange={selectedRange}
                onRangeChange={onRangeChange}
              />
            </div>
          </div>

          <div className="rounded-md border bg-muted/20 px-3 py-2 shrink-0 lg:ml-auto">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Action</div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={onFindTopGainer}
                disabled={actionDisabled}
              >
                Find highest gain
              </Button>
            </div>
          </div>
        </div>

        <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
          <span>Found {foundCount}</span>
          {(loading || yieldsLoading) && (
            <Badge variant="secondary" className="animate-pulse">
              {loading ? 'Loading funds...' : `Calculating yields for ${selectedRange} months...`}
            </Badge>
          )}
        </div>

        {yieldsLoading && (
          <div className="mt-3 space-y-2">
            <div className="flex justify-between text-sm text-muted-foreground">
              <span>Processing yield calculations...</span>
              <span>{Math.round(yieldProgress)}%</span>
            </div>
            <Progress value={yieldProgress} className="h-2" />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
