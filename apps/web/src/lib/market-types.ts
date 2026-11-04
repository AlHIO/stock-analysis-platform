/** Phase 1C OpenAPI projections. Decimal values stay strings until chart rendering. */
export type Timeframe = "1d" | "1w" | "1M";
export type InstrumentType =
  | "stock"
  | "etf"
  | "fund"
  | "index"
  | "preferred_stock"
  | "warrant"
  | "etn"
  | "dr"
  | "reit"
  | "abs";
export type Market = {
  id: string;
  code: string;
  name: string;
  timezone: string;
  currency: string;
};
export type Instrument = {
  id: string;
  market: Market;
  symbol: string;
  name: string;
  instrument_type: InstrumentType;
  quote_currency: string;
  quantity_unit: "shares" | "units";
  status: "active" | "suspended" | "delisted";
  created_at: string;
  updated_at: string;
};
export type InstrumentPage = {
  items: Instrument[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
};
export type Source = {
  id: string;
  code: string;
  name: string;
  source_type: "exchange" | "vendor" | "file" | "manual";
};
export type Freshness = {
  as_of: string;
  market_timezone: string;
  market_local_date: string;
  latest_trade_date: string | null;
  last_ingested_at: string | null;
  age_calendar_days: number | null;
  is_realtime: false;
  assessment: "no_data" | "calendar_not_verified" | "future_dated";
};
export type History = {
  available_from: string | null;
  available_to: string | null;
  completeness: "not_verified";
};
export type Bar = {
  trade_date: string;
  period_start: string;
  period_end: string;
  first_trade_date: string;
  last_trade_date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume_shares: number;
  turnover: string | null;
  trade_count: number | null;
  observed_session_count: number;
  expected_session_count: number | null;
  period_closed: boolean;
  completeness: "complete" | "incomplete" | "unknown";
  is_complete: boolean;
  completeness_reasons: string[];
  missing_trade_dates: string[];
  calendar_missing_dates: string[];
  sources: Source[];
  last_ingested_at: string;
  previous_observed: { trade_date: string; close: string } | null;
  previous_session_verified: boolean | null;
  reference_price: string | null;
  change: string | null;
  change_percent: string | null;
  change_unavailable_reason: "official_reference_not_available";
};
export type LatestResponse = {
  instrument: Instrument;
  bar: Bar | null;
  freshness: Freshness;
  history: History;
  price_adjustment: "unadjusted";
  data_kind: "daily_snapshot";
};
export type BarsResponse = Omit<LatestResponse, "bar"> & {
  timeframe: Timeframe;
  from: string;
  to: string;
  count: number;
  bars: Bar[];
  query_limit: number;
};

type RecordValue = Record<string, unknown>;
const record = (v: unknown): v is RecordValue =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string";
const uuid = (v: unknown): v is string =>
  text(v) && /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i.test(v);
// Pydantic Decimal can emit scientific notation (for example, NUMERIC zero as 0E-8).
const decimal = (v: unknown): v is string =>
  text(v) && /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(v);
const integer = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const date = (v: unknown): v is string =>
  text(v) &&
  /^\d{4}-\d{2}-\d{2}$/.test(v) &&
  !Number.isNaN(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v;
const timestamp = (v: unknown): v is string =>
  text(v) && /^\d{4}-\d{2}-\d{2}T/.test(v) && !Number.isNaN(Date.parse(v));
const nullable = (v: unknown, guard: (v: unknown) => boolean) =>
  v === null || guard(v);
const array = (v: unknown, guard: (v: unknown) => boolean) =>
  Array.isArray(v) && v.every(guard);
const oneOf = (v: unknown, values: readonly unknown[]) => values.includes(v);

export function isInstrument(v: unknown): v is Instrument {
  return (
    record(v) &&
    uuid(v.id) &&
    record(v.market) &&
    uuid(v.market.id) &&
    ["code", "name", "timezone", "currency"].every((key) =>
      text((v.market as RecordValue)[key]),
    ) &&
    text(v.symbol) &&
    text(v.name) &&
    oneOf(v.instrument_type, [
      "stock",
      "etf",
      "fund",
      "index",
      "preferred_stock",
      "warrant",
      "etn",
      "dr",
      "reit",
      "abs",
    ]) &&
    text(v.quote_currency) &&
    /^[A-Z]{3}$/.test(v.quote_currency) &&
    oneOf(v.quantity_unit, ["shares", "units"]) &&
    oneOf(v.status, ["active", "suspended", "delisted"]) &&
    timestamp(v.created_at) &&
    timestamp(v.updated_at)
  );
}
export function isInstrumentPage(v: unknown): v is InstrumentPage {
  return (
    record(v) &&
    array(v.items, isInstrument) &&
    integer(v.total) &&
    integer(v.page) &&
    v.page > 0 &&
    integer(v.page_size) &&
    v.page_size > 0 &&
    integer(v.total_pages)
  );
}
function isSource(v: unknown): v is Source {
  return (
    record(v) &&
    uuid(v.id) &&
    text(v.code) &&
    text(v.name) &&
    oneOf(v.source_type, ["exchange", "vendor", "file", "manual"])
  );
}
function isFreshness(v: unknown): v is Freshness {
  return (
    record(v) &&
    timestamp(v.as_of) &&
    text(v.market_timezone) &&
    date(v.market_local_date) &&
    nullable(v.latest_trade_date, date) &&
    nullable(v.last_ingested_at, timestamp) &&
    nullable(
      v.age_calendar_days,
      (n) => typeof n === "number" && Number.isSafeInteger(n),
    ) &&
    v.is_realtime === false &&
    oneOf(v.assessment, ["no_data", "calendar_not_verified", "future_dated"])
  );
}
function isHistory(v: unknown): v is History {
  return (
    record(v) &&
    nullable(v.available_from, date) &&
    nullable(v.available_to, date) &&
    v.completeness === "not_verified"
  );
}
export function isBar(v: unknown): v is Bar {
  return (
    record(v) &&
    [
      "trade_date",
      "period_start",
      "period_end",
      "first_trade_date",
      "last_trade_date",
    ].every((key) => date(v[key])) &&
    ["open", "high", "low", "close"].every((key) => decimal(v[key])) &&
    integer(v.volume_shares) &&
    nullable(v.turnover, decimal) &&
    nullable(v.trade_count, integer) &&
    integer(v.observed_session_count) &&
    nullable(v.expected_session_count, integer) &&
    typeof v.period_closed === "boolean" &&
    oneOf(v.completeness, ["complete", "incomplete", "unknown"]) &&
    typeof v.is_complete === "boolean" &&
    array(v.completeness_reasons, text) &&
    array(v.missing_trade_dates, date) &&
    array(v.calendar_missing_dates, date) &&
    array(v.sources, isSource) &&
    timestamp(v.last_ingested_at) &&
    nullable(
      v.previous_observed,
      (p) => record(p) && date(p.trade_date) && decimal(p.close),
    ) &&
    nullable(v.previous_session_verified, (p) => typeof p === "boolean") &&
    nullable(v.reference_price, decimal) &&
    nullable(v.change, decimal) &&
    nullable(v.change_percent, decimal) &&
    v.change_unavailable_reason === "official_reference_not_available"
  );
}
function isEnvelope(v: unknown): v is RecordValue {
  return (
    record(v) &&
    isInstrument(v.instrument) &&
    isFreshness(v.freshness) &&
    isHistory(v.history) &&
    v.price_adjustment === "unadjusted" &&
    v.data_kind === "daily_snapshot"
  );
}
export function isLatestResponse(v: unknown): v is LatestResponse {
  return isEnvelope(v) && nullable(v.bar, isBar);
}
export function isBarsResponse(v: unknown): v is BarsResponse {
  return (
    isEnvelope(v) &&
    oneOf(v.timeframe, ["1d", "1w", "1M"]) &&
    date(v.from) &&
    date(v.to) &&
    integer(v.count) &&
    array(v.bars, isBar) &&
    v.count === (v.bars as Bar[]).length &&
    integer(v.query_limit) &&
    v.query_limit > 0
  );
}
