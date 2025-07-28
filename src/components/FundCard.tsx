import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Fund } from "@/services/investmentApi";

interface FundCardProps {
  fund: Fund;
  yieldPercent?: string;
  onClick: () => void;
}

export const FundCard = ({ fund, yieldPercent, onClick }: FundCardProps) => {
  const getDeviceClassColor = (deviceClass: string) => {
    switch (deviceClass) {
      case 'STOCK_MATERIAL':
        return 'bg-destructive/10 text-destructive hover:bg-destructive/20';
      case 'BOND':
        return 'bg-primary/10 text-primary hover:bg-primary/20';
      case 'MIXED':
        return 'bg-secondary/10 text-secondary-foreground hover:bg-secondary/20';
      case 'MONEY_MARKET':
        return 'bg-muted/10 text-muted-foreground hover:bg-muted/20';
      default:
        return 'bg-muted/10 text-muted-foreground hover:bg-muted/20';
    }
  };

  const getSustainabilityColor = (sustainability: string) => {
    return sustainability === 'RESPONSIBLE_FUTURE' 
      ? 'bg-green-100 text-green-800 hover:bg-green-200 dark:bg-green-900 dark:text-green-300'
      : 'bg-gray-100 text-gray-800 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300';
  };

  return (
    <Card 
      className="cursor-pointer transition-all duration-200 hover:shadow-lg hover:scale-[1.02] border-border/50"
      onClick={onClick}
    >
      <CardHeader className="pb-3">
        <div className="flex justify-between items-start gap-2">
          <CardTitle className="text-base font-medium leading-tight">
            {fund.portfolioName}
          </CardTitle>
          {yieldPercent && (
            <Badge variant="secondary" className="font-mono text-sm font-semibold">
              {yieldPercent}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1">
            <Badge 
              variant="outline" 
              className={getDeviceClassColor(fund.deviceClassType)}
            >
              {fund.deviceClassType.replace('_', ' ')}
            </Badge>
            <Badge 
              variant="outline"
              className={getSustainabilityColor(fund.sustainabilityType)}
            >
              {fund.sustainabilityType === 'RESPONSIBLE_FUTURE' ? 'ESG' : 'Traditional'}
            </Badge>
          </div>
          
          <div className="text-sm text-muted-foreground space-y-1">
            <div className="flex justify-between">
              <span>Currency:</span>
              <span className="font-medium">{fund.currencyType}</span>
            </div>
            <div className="flex justify-between">
              <span>Type:</span>
              <span className="font-medium">{fund.investmentType}</span>
            </div>
            <div className="flex justify-between">
              <span>Fund No:</span>
              <span className="font-mono text-xs">{fund.fundNo}</span>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};