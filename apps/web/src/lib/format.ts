import { expandDecimal } from "./decimal";
import type { Timeframe } from "./market-types";

/** Financial values stay decimal strings; grouping never rounds through Number. */
export function decimal(value: string | null | undefined): string {
  if (value == null) return "—";
  const expanded = expandDecimal(value);
  if (expanded == null) return "—";
  const [integer, fraction = ""] = expanded.split(".");
  const trimmed = fraction.replace(/0+$/, "");
  return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${trimmed ? `.${trimmed}` : ""}`;
}

export function shares(value: number | null | undefined): string {
  return value != null && Number.isSafeInteger(value) && value >= 0
    ? value.toLocaleString("en-US")
    : "—";
}

export function dateLabel(value: string | null | undefined): string {
  return value ? value.replaceAll("-", "/") : "—";
}

export function timestamp(
  value: string | null | undefined,
  timezone = "Asia/Taipei",
): string {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-TW", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(value));
  } catch {
    return "—";
  }
}

export function marketLabel(code: string): string {
  return (
    ({ TWSE: "上市", TPEX: "上櫃" } as Record<string, string>)[code] ?? code
  );
}

export function changeDirection(
  value: string | null | undefined,
): "up" | "down" | "neutral" {
  const expanded = value == null ? null : expandDecimal(value);
  if (!expanded || !/[1-9]/.test(expanded)) return "neutral";
  return expanded.startsWith("-") ? "down" : "up";
}

/** Calendar arithmetic for a bounded query, never a trade-session assumption. */
export function recentRange(
  from: string | null,
  to: string | null,
): { from: string; to: string } | undefined {
  if (!to) return undefined;
  const end = new Date(`${to}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) return undefined;
  end.setUTCDate(end.getUTCDate() - 365);
  const start = end.toISOString().slice(0, 10);
  return { from: from && from > start ? from : start, to };
}

/** Full initial calendar bucket keeps chart candles aligned with indicators. */
export function periodAlignedRange(
  range: { from: string; to: string } | undefined,
  timeframe: Timeframe,
) {
  if (!range || timeframe === "1d") return range;
  const start = new Date(`${range.from}T00:00:00Z`);
  if (timeframe === "1w")
    start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
  else start.setUTCDate(1);
  return { ...range, from: start.toISOString().slice(0, 10) };
}
