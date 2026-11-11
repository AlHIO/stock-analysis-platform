import type { HistogramData, LineData, Time, WhitespaceData } from "lightweight-charts";
import { expandDecimal } from "./decimal";

export interface ChipDatum {
  date: string;
  value: string | null;
  status?: string;
}

export interface ChipSeries {
  id: string;
  label: string;
  color: string;
  kind: "line" | "histogram";
  points: readonly ChipDatum[];
}

export interface ConvertedChipSeries {
  source: ChipSeries;
  data: (LineData<Time> | HistogramData<Time> | WhitespaceData<Time>)[];
  segments: LineData<Time>[][];
  exactByDate: ReadonlyMap<string, ChipDatum>;
  precision: number;
}

export class ChipChartDataError extends Error {
  constructor() {
    super("籌碼圖表資料無法安全繪製；原始精確數值不會轉成假資料。");
    this.name = "ChipChartDataError";
  }
}

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function numberForPixels(value: string): { value: number; canonical: string; precision: number } {
  if (typeof value !== "string") throw new ChipChartDataError();
  const expanded = expandDecimal(value);
  if (expanded === null) throw new ChipChartDataError();
  const negative = expanded.startsWith("-");
  const [whole, rawFraction = ""] = expanded.replace(/^-/, "").split(".");
  const fraction = rawFraction.replace(/0+$/, "");
  const magnitude = BigInt(whole);
  if (magnitude > BigInt(Number.MAX_SAFE_INTEGER) ||
      (magnitude === BigInt(Number.MAX_SAFE_INTEGER) && fraction !== "")) {
    throw new ChipChartDataError();
  }
  const numeric = Number(expanded); // Rendering only; labels retain exact API strings.
  const nonzero = /[1-9]/.test(whole + fraction);
  if (!Number.isFinite(numeric) || (nonzero && numeric === 0)) throw new ChipChartDataError();
  return {
    value: numeric,
    canonical: `${negative && nonzero ? "-" : ""}${whole}${fraction ? `.${fraction}` : ""}`,
    precision: Math.min(6, fraction.length),
  };
}

/**
 * Transform real observed dates only. No daily resampling or absent-day zeroes.
 * Explicit null observations are whitespace and split line segments so the
 * renderer cannot bridge a known missing input. IEEE-754 is used only for pixels.
 */
export function toChipChartData(series: readonly ChipSeries[]): ConvertedChipSeries[] {
  const ids = new Set<string>();
  return series.map((source) => {
    if (typeof source.id !== "string" || !source.id || ids.has(source.id) ||
        !Array.isArray(source.points) || !["line", "histogram"].includes(source.kind)) throw new ChipChartDataError();
    ids.add(source.id);
    const data: ConvertedChipSeries["data"] = [];
    const segments: LineData<Time>[][] = [];
    const exactByDate = new Map<string, ChipDatum>();
    const exactByPixel = new Map<number, string>();
    let previous: string | null = null;
    let segment: LineData<Time>[] = [];
    let precision = 0;
    for (const point of source.points) {
      if (!validDate(point.date) || previous !== null && point.date <= previous) {
        throw new ChipChartDataError();
      }
      previous = point.date;
      exactByDate.set(point.date, point);
      if (point.value === null) {
        data.push({ time: point.date });
        if (segment.length) segments.push(segment);
        segment = [];
        continue;
      }
      const number = numberForPixels(point.value);
      const prior = exactByPixel.get(number.value);
      if (prior !== undefined && prior !== number.canonical) throw new ChipChartDataError();
      exactByPixel.set(number.value, number.canonical);
      precision = Math.max(precision, number.precision);
      const datum = { time: point.date, value: number.value };
      data.push(source.kind === "histogram"
        ? { ...datum, color: number.value < 0 ? "#58c9a6" : source.color }
        : datum);
      segment.push(datum);
    }
    if (segment.length) segments.push(segment);
    return { source, data, segments, exactByDate, precision };
  });
}
