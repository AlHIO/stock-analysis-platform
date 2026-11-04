import type {
  HistogramData,
  LineData,
  Time,
  WhitespaceData,
} from "lightweight-charts";
import type { Bar, Timeframe } from "./market-types";
import type { IndicatorName, IndicatorResponse } from "./technical-types";
import { expandDecimal } from "./decimal";

export const INDICATOR_COLORS: Record<IndicatorName, string> = {
  sma: "#e8bd74",
  ema: "#79b7fa",
  rsi: "#bf9bfa",
  macd: "#79b7fa",
  volume_sma: "#e8bd74",
};
export type IndicatorChartPoint = LineData<Time> | WhitespaceData<Time>;
export type IndicatorComponent = {
  key: string;
  label: string;
  color: string;
  histogram: boolean;
  data: IndicatorChartPoint[];
  segments: LineData<Time>[][];
  precision: number;
};
export type ConvertedIndicator = {
  source: IndicatorResponse;
  label: string;
  components: IndicatorComponent[];
  pointsByDate: Map<string, IndicatorResponse["points"][number]>;
};
export class IndicatorChartError extends Error {
  constructor() {
    super("部分指標數值無法安全繪圖；價格與成交量仍可檢視。");
    this.name = "IndicatorChartError";
  }
}

export function indicatorLabel(indicator: IndicatorResponse): string {
  const p = indicator.parameters;
  return indicator.indicator === "macd"
    ? `MACD(${p.fast_period},${p.slow_period},${p.signal_period})`
    : `${({ sma: "SMA", ema: "EMA", rsi: "RSI", volume_sma: "VOL MA" } as const)[indicator.indicator]}(${p.period})`;
}

function normalizedDecimal(value: string): string {
  const expanded = expandDecimal(value);
  if (expanded === null) throw new IndicatorChartError();
  const [integer, fraction = ""] = expanded.split(".");
  const trimmed = fraction.replace(/0+$/, "");
  return trimmed ? `${integer}.${trimmed}` : integer;
}

function validDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 10) === value
  );
}

/** No formulas here: only validate server output and adapt values to chart pixels. */
export function toIndicatorChartData(
  indicators: readonly IndicatorResponse[],
  bars: readonly Bar[],
  timeframe: Timeframe,
): ConvertedIndicator[] {
  const dates = [...new Set(bars.map((bar) => bar.trade_date))].sort();
  if (!dates.length) return [];
  const candleDates = new Set(dates);
  const names = new Set<IndicatorName>();
  return indicators.map((source) => {
    if (
      source.timeframe !== timeframe ||
      source.price_basis !== "raw" ||
      names.has(source.indicator)
    )
      throw new IndicatorChartError();
    names.add(source.indicator);
    const pointsByDate = new Map<string, IndicatorResponse["points"][number]>();
    for (const point of source.points) {
      if (!validDate(point.trade_date) || pointsByDate.has(point.trade_date))
        throw new IndicatorChartError();
      pointsByDate.set(point.trade_date, point);
    }
    const ordered = [...new Set([...dates, ...pointsByDate.keys()])]
      .filter((date) => date >= dates[0] && date <= dates[dates.length - 1])
      .sort();
    const keys =
      source.indicator === "macd"
        ? ["macd", "signal", "histogram"]
        : [source.indicator];
    const components = keys.map((key) => {
      const color =
        key === "signal" ? "#e8bd74" : INDICATOR_COLORS[source.indicator];
      const component: IndicatorComponent = {
        key,
        label: key.toUpperCase(),
        color,
        histogram: key === "histogram",
        data: [],
        segments: [],
        precision: 2,
      };
      const exactByNumber = new Map<number, string>();
      let segment: LineData<Time>[] = [];
      const finishSegment = () => {
        if (segment.length) component.segments.push(segment);
        segment = [];
      };
      for (const date of ordered) {
        const point = pointsByDate.get(date);
        const raw = point?.values[key] ?? null;
        if (raw === null) {
          finishSegment();
          if (candleDates.has(date)) component.data.push({ time: date });
          continue;
        }
        if (typeof raw !== "string" || !candleDates.has(date))
          throw new IndicatorChartError();
        const exact = normalizedDecimal(raw);
        const value = Number(exact); // Rendering boundary only; labels use the original string.
        if (
          !Number.isFinite(value) ||
          Math.abs(value) > Number.MAX_SAFE_INTEGER ||
          (value === 0 && /[1-9]/.test(exact))
        )
          throw new IndicatorChartError();
        if (
          (source.indicator === "sma" || source.indicator === "ema") &&
          value <= 0
        )
          throw new IndicatorChartError();
        if (source.indicator === "volume_sma" && value < 0)
          throw new IndicatorChartError();
        if (source.indicator === "rsi" && (value < 0 || value > 100))
          throw new IndicatorChartError();
        const seen = exactByNumber.get(value);
        if (seen !== undefined && seen !== exact)
          throw new IndicatorChartError();
        exactByNumber.set(value, exact);
        component.precision = Math.min(
          12,
          Math.max(component.precision, (exact.split(".")[1] ?? "").length),
        );
        const entry = { time: date, value };
        component.data.push(
          component.histogram
            ? ({
                ...entry,
                color: value >= 0 ? "#f2758280" : "#4bc6a080",
              } as HistogramData<Time>)
            : entry,
        );
        segment.push(entry);
      }
      finishSegment();
      // Prevent corrupted input from creating an excessive number of chart series.
      if (component.segments.length > 128) throw new IndicatorChartError();
      return component;
    });
    return { source, label: indicatorLabel(source), components, pointsByDate };
  });
}
