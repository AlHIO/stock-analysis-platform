/** Presentation only: calendar ranges, missing points and backend status labels. */
import type { ChipSeries } from "./chip-chart";
import type { ChipCalculation, ChipQuantity, ChipRange } from "./chip-types";

export function chipRange(end: string, days: number): ChipRange {
  const to = new Date(`${end}T00:00:00Z`);
  to.setUTCDate(to.getUTCDate() - days);
  return { from: to.toISOString().slice(0, 10), to: end };
}

export function validChipRange(range: ChipRange, marketDate: string): string | null {
  for (const day of [range.from, range.to]) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return "請輸入完整日期。";
    const value = new Date(`${day}T00:00:00Z`);
    if (!Number.isFinite(value.getTime()) || value.toISOString().slice(0,10) !== day) return "日期無效。";
  }
  if (range.from > range.to) return "開始日期不可晚於結束日期。";
  if (range.to > marketDate) return "結束日期不可晚於市場當地日期。";
  if ((Date.parse(range.to)-Date.parse(range.from))/86400000 > 365) return "每次最多顯示 366 個日曆日，請縮短區間。";
  return null;
}

export function calculationValue(value?: ChipCalculation | null): string | null {
  return value?.status === "complete" ? value.value : null;
}

export function quantityValue(value?: ChipQuantity): string | null {
  return value?.availability === "reported" ? value.value : null;
}

export function chipStatus(status: string | null | undefined): string {
  const labels: Record<string,string> = {
    complete: "已核對計算", verified: "已核對", partial: "Partial · 來源欄位不完整",
    not_verified: "尚未核實", unavailable: "Unavailable", missing_data: "缺少資料",
    insufficient_history: "歷史資料不足", truncated_history: "連續區間左端未確認",
    no_sessions: "尚無確認交易日", incompatible_data: "來源口徑不相容",
    unsupported: "來源不支援此計算", not_applicable: "此報表不適用",
    invalid: "數據驗證未通過", not_yet_available: "當時尚不可取得",
  };
  return status ? labels[status] ?? "資料狀態待核對" : "Unavailable";
}

/** Union of actual daily observations only; missing values remain whitespace. */
export function alignDailySeries(series: readonly ChipSeries[]): ChipSeries[] {
  const dates = [...new Set(series.flatMap(s => s.points.map(p => p.date)))].sort();
  return series.map(s => {
    const byDate = new Map(s.points.map(p => [p.date,p]));
    if (byDate.size !== s.points.length) throw new Error("Duplicate chip dates");
    return {...s,points:dates.map(date => byDate.get(date) ?? {date,value:null,status:"missing_data"})};
  });
}
