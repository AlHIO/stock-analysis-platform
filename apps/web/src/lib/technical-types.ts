import {
  isLatestResponse,
  type LatestResponse,
  type Timeframe,
} from "./market-types";
import { expandDecimal } from "./decimal";

export const INDICATOR_NAMES = [
  "sma",
  "ema",
  "rsi",
  "macd",
  "volume_sma",
] as const;
export type IndicatorName = (typeof INDICATOR_NAMES)[number];
export type IndicatorPoint = {
  trade_date: string;
  values: Record<string, string | null>;
  status: "ready" | "warmup" | "missing_input";
  is_provisional: boolean;
};
export type IndicatorResponse = Omit<LatestResponse, "bar"> & {
  timeframe: Timeframe;
  indicator: IndicatorName;
  algorithm_version: string;
  parameters: Record<string, number>;
  price_basis: "raw";
  field: "close" | "volume_shares";
  required_samples: number;
  from: string;
  to: string;
  points: IndicatorPoint[];
  count: number;
  precision: {
    internal_digits: 50;
    output_decimal_places: 12;
    rounding: "ROUND_HALF_EVEN";
  };
  calculation: {
    seed_policy: "full_available_history";
    history_from: string | null;
    input_count: number;
    prehistory_count: number;
    missing_policy: "reset_on_explicit_missing";
    calendar_policy: "observed_sessions_with_known_gaps";
    history_completeness: "not_verified";
  };
};

const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const date = (v: unknown): v is string =>
  typeof v === "string" &&
  /^\d{4}-\d{2}-\d{2}$/.test(v) &&
  Number.isFinite(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v;
export function isIndicatorResponse(v: unknown): v is IndicatorResponse {
  if (
    !record(v) ||
    !isLatestResponse({ ...v, bar: null }) ||
    !INDICATOR_NAMES.includes(v.indicator as IndicatorName) ||
    !["1d", "1w", "1M"].includes(v.timeframe as string) ||
    v.algorithm_version !== "1.0.0" ||
    v.price_basis !== "raw" ||
    v.field !== (v.indicator === "volume_sma" ? "volume_shares" : "close") ||
    !integer(v.required_samples) ||
    v.required_samples < 1 ||
    !date(v.from) ||
    !date(v.to) ||
    (typeof v.from === "string" && typeof v.to === "string" && v.from > v.to) ||
    !integer(v.count) ||
    !Array.isArray(v.points) ||
    v.count !== v.points.length ||
    v.count > 1000 ||
    !record(v.parameters) ||
    !record(v.precision) ||
    !record(v.calculation)
  )
    return false;
  const keys =
    v.indicator === "macd"
      ? ["fast_period", "slow_period", "signal_period"]
      : ["period"];
  const values =
    v.indicator === "macd"
      ? ["macd", "signal", "histogram"]
      : [v.indicator as string];
  if (
    Object.keys(v.parameters).length !== keys.length ||
    !keys.every(
      (key) =>
        integer((v.parameters as Record<string, unknown>)[key]) &&
        Number((v.parameters as Record<string, unknown>)[key]) >= 1 &&
        Number((v.parameters as Record<string, unknown>)[key]) <= 250,
    )
  )
    return false;
  if (
    v.indicator === "macd" &&
    Number(v.parameters.fast_period) >= Number(v.parameters.slow_period)
  )
    return false;
  if (
    v.precision.internal_digits !== 50 ||
    v.precision.output_decimal_places !== 12 ||
    v.precision.rounding !== "ROUND_HALF_EVEN"
  )
    return false;
  const calc = v.calculation;
  if (
    calc.seed_policy !== "full_available_history" ||
    (calc.history_from !== null && !date(calc.history_from)) ||
    !integer(calc.input_count) ||
    !integer(calc.prehistory_count) ||
    calc.prehistory_count > calc.input_count ||
    calc.missing_policy !== "reset_on_explicit_missing" ||
    calc.calendar_policy !== "observed_sessions_with_known_gaps" ||
    calc.history_completeness !== "not_verified"
  )
    return false;
  let previous = "";
  for (const point of v.points) {
    if (
      !record(point) ||
      !date(point.trade_date) ||
      point.trade_date <= previous ||
      point.trade_date < v.from ||
      point.trade_date > v.to ||
      typeof point.is_provisional !== "boolean" ||
      !["ready", "warmup", "missing_input"].includes(point.status as string) ||
      !record(point.values) ||
      Object.keys(point.values).length !== values.length ||
      !values.every(
        (key) =>
          (point.values &&
            (point.values as Record<string, unknown>)[key] === null) ||
          (record(point.values) &&
            typeof point.values[key] === "string" &&
            expandDecimal(point.values[key] as string) !== null),
      )
    )
      return false;
    if (
      point.status === "ready" &&
      values.some(
        (key) => (point.values as Record<string, unknown>)[key] === null,
      )
    )
      return false;
    if (
      point.status === "missing_input" &&
      values.some(
        (key) => (point.values as Record<string, unknown>)[key] !== null,
      )
    )
      return false;
    if (
      point.status === "warmup" &&
      values.every(
        (key) => (point.values as Record<string, unknown>)[key] !== null,
      )
    )
      return false;
    previous = point.trade_date;
  }
  return true;
}
