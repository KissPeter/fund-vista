import { Button } from "@/components/ui/button";

interface DateRangeFilterProps {
  selectedRange: number;
  onRangeChange: (months: number) => void;
}

const DATE_RANGES = [
  { label: '1M', months: 1 },
  { label: '3M', months: 3 },
  { label: '6M', months: 6 },
  { label: '1Y', months: 12 }
];

export const DateRangeFilter = ({ selectedRange, onRangeChange }: DateRangeFilterProps) => {
  return (
    <div className="flex gap-2">
      {DATE_RANGES.map(({ label, months }) => (
        <Button
          key={months}
          variant={selectedRange === months ? "default" : "outline"}
          size="sm"
          onClick={() => onRangeChange(months)}
          className="min-w-[60px]"
        >
          {label}
        </Button>
      ))}
    </div>
  );
};