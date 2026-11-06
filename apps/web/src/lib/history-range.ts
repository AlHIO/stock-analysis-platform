import { periodAlignedRange } from "./format";
import type { Timeframe } from "./market-types";

export type HistoryRange = { from: string; to: string };
export type HistoryPreset = "1y" | "5y" | "10y";

/** Mirrors the existing API contract; these are calendar days, not sessions. */
export const MAX_HISTORY_RANGE_DAYS = 3660;
const DAY_MS = 86_400_000;
const PRESET_YEARS: Record<HistoryPreset, number> = {
  "1y": 1,
  "5y": 5,
  "10y": 10,
};

function calendarDate(value: unknown): Date | undefined {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value))
    return undefined;
  if (value.slice(0, 4) === "0000") return undefined;
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime())) return undefined;
  // Date otherwise accepts values such as February 30 by rolling into March.
  return parsed.toISOString().slice(0, 10) === value ? parsed : undefined;
}

function label(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function earliestQueryDate(end: Date): string {
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - MAX_HISTORY_RANGE_DAYS + 1);
  return start.getUTCFullYear() < 1 ? "0001-01-01" : label(start);
}

function yearsBefore(end: Date, years: number): string {
  const year = end.getUTCFullYear() - years;
  if (year < 1) return "0001-01-01";
  const start = new Date(end);
  const day = start.getUTCDate();
  start.setUTCDate(1);
  start.setUTCFullYear(year);
  const lastDay = new Date(start);
  lastDay.setUTCMonth(lastDay.getUTCMonth() + 1, 0);
  start.setUTCDate(Math.min(day, lastDay.getUTCDate()));
  return label(start);
}

/** Anchor presets to saved coverage, without inventing missing trading days. */
export function presetHistoryRange(
  availableFrom: string | null,
  availableTo: string | null,
  preset: HistoryPreset,
): HistoryRange | undefined {
  const end = calendarDate(availableTo);
  const first = availableFrom == null ? undefined : calendarDate(availableFrom);
  const years = PRESET_YEARS[preset];
  if (
    !end ||
    (availableFrom != null && !first) ||
    (first && first > end) ||
    typeof years !== "number"
  )
    return undefined;
  const from = [
    yearsBefore(end, years),
    earliestQueryDate(end),
    ...(availableFrom == null ? [] : [availableFrom]),
  ].sort().at(-1)!;
  return { from, to: availableTo! };
}

export function validateHistoryRange(range: HistoryRange): string | null {
  const start = calendarDate(range?.from);
  const end = calendarDate(range?.to);
  if (!start || !end) return "請輸入有效的起訖日期。";
  if (start > end) return "開始日期不可晚於結束日期。";
  if ((end.getTime() - start.getTime()) / DAY_MS + 1 > MAX_HISTORY_RANGE_DAYS)
    return "單次查詢最多 3,660 個日曆日，請縮短日期範圍。";
  return null;
}

/** A conservative display choice, independent of how many bars are saved. */
export function recommendedHistoryTimeframe(range: HistoryRange): Timeframe {
  const start = calendarDate(range?.from);
  const end = calendarDate(range?.to);
  return start && end && (end.getTime() - start.getTime()) / DAY_MS + 1 > 1000
    ? "1w"
    : "1d";
}

/**
 * Keep chart and indicator queries on the same initial bucket. If a whole
 * bucket would exceed the API's calendar limit, reject the range instead of
 * starting mid-bucket and giving the chart a different prefix from indicators.
 * No prices or sessions are filled here.
 */
export function alignedHistoryRange(
  range: HistoryRange | undefined,
  timeframe: Timeframe,
): HistoryRange | undefined {
  if (
    !range ||
    validateHistoryRange(range) != null ||
    !["1d", "1w", "1M"].includes(timeframe)
  )
    return undefined;
  const aligned = periodAlignedRange(range, timeframe)!;
  return validateHistoryRange(aligned) == null ? aligned : undefined;
}
