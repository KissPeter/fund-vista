import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { FundCard } from "@/components/FundCard";
import type { Fund } from "@/services/investmentApi";

interface FundGridProps {
  loading: boolean;
  funds: Fund[];
  fundYields: Record<number, string>;
  yieldsLoading: boolean;
  onFundClick: (fund: Fund) => void;
}

/**
 * Fund card grid with loading skeletons (REF-002 Phase 5).
 *
 * Pure move of Index's funds grid; presentational only.
 */
export function FundGrid({
  loading,
  funds,
  fundYields,
  yieldsLoading,
  onFundClick,
}: FundGridProps) {
  if (loading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {[...Array(6)].map((_, i) => (
          <Card key={i} className="animate-pulse">
            <CardHeader>
              <div className="h-4 bg-muted rounded w-3/4"></div>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                <div className="h-3 bg-muted rounded w-full"></div>
                <div className="h-3 bg-muted rounded w-2/3"></div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {funds.map((fund) => (
        <FundCard
          key={fund.primaryKey}
          fund={fund}
          yieldPercent={fundYields[fund.primaryKey]}
          isLoadingYield={yieldsLoading}
          onClick={() => onFundClick(fund)}
        />
      ))}
    </div>
  );
}
