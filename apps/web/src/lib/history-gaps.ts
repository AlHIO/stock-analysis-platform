import type { Bar } from "./market-types";

export type ObservedHistoryGap = {
  previousDate: string;
  nextDate: string;
  calendarDays: number;
};

/** An attention threshold for observed dates, not an exchange-calendar rule. */
export const HISTORY_GAP_ATTENTION_DAYS = 90;
const DAY_MS = 86_400_000;

function invalidDates(): never {
  throw new RangeError("Invalid observed history dates");
}

function localDate(value: unknown): number {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
    value.startsWith("0000-")
  )
    return invalidDates();
  const parsed = new Date(`${value}T00:00:00Z`);
  const timestamp = parsed.getTime();
  if (
    !Number.isFinite(timestamp) ||
    parsed.toISOString().slice(0, 10) !== value
  )
    return invalidDates();
  return timestamp;
}

/**
 * Describe long intervals between observations already returned by the API.
 * A date interval does not prove missing trading sessions, a suspension or a
 * holiday. Aggregate boundaries use actual first/last observations, never the
 * enclosing week's/month's theoretical boundaries. No data is filled or erased.
 */
export function analyzeHistoryGaps(bars: readonly Bar[]): {
  gaps: ObservedHistoryGap[];
  recentFrom: string | null;
} {
  const observed = bars.map((bar) => {
    const first = localDate(bar.first_trade_date);
    const last = localDate(bar.last_trade_date);
    const displayed = localDate(bar.trade_date);
    if (first > last || displayed !== last) invalidDates();
    return { bar, first, last };
  });
  observed.sort((left, right) => left.last - right.last);

  const gaps: ObservedHistoryGap[] = [];
  let recentFrom = observed[0]?.bar.first_trade_date ?? null;
  for (let index = 1; index < observed.length; index++) {
    const previous = observed[index - 1];
    const next = observed[index];
    if (next.first <= previous.last) invalidDates();
    const calendarDays = (next.first - previous.last) / DAY_MS;
    if (calendarDays > HISTORY_GAP_ATTENTION_DAYS) {
      gaps.push({
        previousDate: previous.bar.last_trade_date,
        nextDate: next.bar.first_trade_date,
        calendarDays,
      });
      recentFrom = next.bar.first_trade_date;
    }
  }
  return { gaps, recentFrom };
}
