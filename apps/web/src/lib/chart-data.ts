import type { CandlestickData, HistogramData, Time } from "lightweight-charts";
import type { Bar } from "./market-types";
import { expandDecimal } from "./decimal";

export const UP_COLOR = "#f27582";
export const DOWN_COLOR = "#4bc6a0";

export class ChartDataError extends Error {
  constructor() {
    super("行情資料無法安全繪製，請稍後重試。");
    this.name = "ChartDataError";
  }
}

/** Compare positive decimal strings without rounding them through a JS number. */
function decimalParts(value: string): [string, string] {
  const expanded = expandDecimal(value);
  if (!expanded || expanded.startsWith("-")) throw new ChartDataError();
  const [integer, fraction = ""] = expanded.split(".");
  const normalized = integer.replace(/^0+(?=\d)/, "");
  if (normalized === "0" && !/[1-9]/.test(fraction)) throw new ChartDataError();
  return [normalized, fraction];
}

export function comparePrices(left: string, right: string): number {
  const [li, lf] = decimalParts(left);
  const [ri, rf] = decimalParts(right);
  if (li.length !== ri.length) return li.length > ri.length ? 1 : -1;
  if (li !== ri) return li > ri ? 1 : -1;
  const length = Math.max(lf.length, rf.length);
  const l = lf.padEnd(length, "0");
  const r = rf.padEnd(length, "0");
  return l === r ? 0 : l > r ? 1 : -1;
}

function chartPrice(value: string): number {
  decimalParts(value);
  const number = Number(value);
  if (
    !Number.isFinite(number) ||
    number <= 0 ||
    number > Number.MAX_SAFE_INTEGER
  ) {
    throw new ChartDataError();
  }
  return number;
}

function isTradeDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return (
    Number.isFinite(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === value
  );
}

export interface ChartData {
  candles: CandlestickData<Time>[];
  volume: HistogramData<Time>[];
  barsByDate: Map<string, Bar>;
}

/**
 * The chart library needs IEEE-754 numbers to position pixels. Conversion happens
 * only here; tooltips and all financial text retain the API's decimal strings.
 * Existing observed trade dates are used unchanged; gaps receive no fake bars.
 */
export function toChartData(bars: readonly Bar[]): ChartData {
  const data: ChartData = { candles: [], volume: [], barsByDate: new Map() };
  const exactPricesByNumber = new Map<number, string>();
  const ordered = [...bars].sort((a, b) =>
    a.trade_date.localeCompare(b.trade_date),
  );
  for (const bar of ordered) {
    if (!isTradeDate(bar.trade_date) || data.barsByDate.has(bar.trade_date)) {
      throw new ChartDataError();
    }
    if (!Number.isSafeInteger(bar.volume_shares) || bar.volume_shares < 0) {
      throw new ChartDataError();
    }
    if (
      comparePrices(bar.low, bar.open) > 0 ||
      comparePrices(bar.open, bar.high) > 0 ||
      comparePrices(bar.low, bar.close) > 0 ||
      comparePrices(bar.close, bar.high) > 0
    )
      throw new ChartDataError();

    const open = chartPrice(bar.open);
    const high = chartPrice(bar.high);
    const low = chartPrice(bar.low);
    const close = chartPrice(bar.close);
    // Distinct exact prices must not collapse within a bar or across the series.
    const prices: [string, number][] = [
      [bar.open, open],
      [bar.high, high],
      [bar.low, low],
      [bar.close, close],
    ];
    for (const [exact, approximate] of prices) {
      const existing = exactPricesByNumber.get(approximate);
      if (existing !== undefined && comparePrices(existing, exact) !== 0) {
        throw new ChartDataError();
      }
      exactPricesByNumber.set(approximate, exact);
    }
    data.candles.push({ time: bar.trade_date, open, high, low, close });
    data.volume.push({
      time: bar.trade_date,
      value: bar.volume_shares,
      color:
        comparePrices(bar.close, bar.open) >= 0 ? "#f2758270" : "#4bc6a070",
    });
    data.barsByDate.set(bar.trade_date, bar);
  }
  return data;
}

export function displayPrice(value: string): string {
  const expanded = expandDecimal(value);
  if (expanded == null) return "—";
  const [integer, fraction = ""] = expanded.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const trimmed = fraction.replace(/0+$/, "");
  return trimmed ? `${grouped}.${trimmed}` : grouped;
}

export function chartPricePrecision(bars: readonly Bar[]): number {
  return Math.max(
    2,
    ...bars.flatMap((bar) =>
      [bar.open, bar.high, bar.low, bar.close].map((price) => {
        const expanded = expandDecimal(price);
        if (expanded == null) throw new ChartDataError();
        return (expanded.split(".")[1] ?? "").replace(/0+$/, "").length;
      }),
    ),
  );
}

export function chartTimeKey(time: Time): string {
  if (typeof time === "string") return time;
  if (typeof time === "object") {
    return `${String(time.year).padStart(4, "0")}-${String(time.month).padStart(2, "0")}-${String(time.day).padStart(2, "0")}`;
  }
  return ""; // We supply BusinessDay strings, never UTC timestamps.
}
