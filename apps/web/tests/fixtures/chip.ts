/**
 * SYNTHETIC fixtures for offline unit/browser tests only. These do not describe
 * real securities, exchange calendars, source permission or production coverage.
 */
import { barFixture, barsFixture, instrumentFixture, latestFixture } from "./market-api";

export const chipInstrumentFixture = {
  ...instrumentFixture,
  symbol: "SYNTHETIC",
  name: "籌碼合成測試股票（非真實行情）",
};
export const CHIP_DATES = ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16", "2026-01-19"];
export const CHIP_WEEKS = ["2026-01-09", "2026-01-16", "2026-01-23"];
const ratioByDate: Record<string, string> = { "2026-01-09": "60.000000000000", "2026-01-16": "65.000000000000", "2026-01-23": "70.000000000000" };
const AT = "2026-01-23T12:00:00Z";
const UUID = "00000000-0000-0000-0000-000000000004";
export const chipSourceFixture = {
  id: "00000000-0000-0000-0000-000000000003",
  code: "SYNTHETIC_CHIP_FIXTURE",
  name: "SYNTHETIC 合成測試來源（非真實籌碼）",
};
export const chipScopeFixture = {
  code: "synthetic_confirmed_scope", includes_block_trades: true,
  includes_odd_lots: true, includes_after_hours: true, basis: "original",
};
export const chipProvenanceFixture = {
  source: chipSourceFixture, revision_id: UUID, run_id: UUID, raw_id: UUID,
  source_row_locator: "SYNTHETIC/fixture/row", parser_version: "SYNTHETIC-parser-v1",
  source_contract_version: "SYNTHETIC-chip-v1", promotion_policy_version: "SYNTHETIC-promotion-v1",
  normalized_hash: "a".repeat(64), payload_sha256: "a".repeat(64),
  schema_version: "SYNTHETIC-schema-v1", identity_evidence_version: "SYNTHETIC-reviewed-v1",
  formula_version: "SYNTHETIC-formula-v1", taxonomy_version: "SYNTHETIC-taxonomy-v1",
  conversion_evidence: null,
};
const quality = {
  status: "verified", flags: [], unavailable_reasons: [], missing_components: [],
  missing_fields: [], missing_bucket_codes: [], domain_completeness: "complete",
};
function timing(day: string) {
  return { as_of_date: day, trade_date: day, source_published_on: null,
    source_published_at: null, first_seen_at: AT, last_seen_at: AT,
    ingested_at: AT, timezone_name: "Asia/Taipei", date_evidence: "source_field",
    date_evidence_version: "SYNTHETIC-date-v1", date_evidence_payload_sha256: null };
}
function freshness(frequency: "1d" | "1w", day: string) {
  return { latest_as_of_date: day, latest_trade_date: day,
    latest_source_published_at: null, first_seen_at: AT, last_seen_at: AT,
    age_calendar_days: 4, assessment: "not_verified", expected_frequency: frequency,
    is_realtime: false };
}
function coverage(from: string, to: string, count: number) {
  return { stored_from: from, stored_to: to, stored_report_count: count,
    completeness: "not_verified", declared_scope: "SYNTHETIC-test-only", expected_dates_known: false,
    known_missing_dates: [], truncated: false };
}
function base<T>(product: string, frequency: "1d" | "1w", items: T[]) {
  const first = frequency === "1w" ? CHIP_WEEKS[0] : CHIP_DATES[0];
  const last = frequency === "1w" ? CHIP_WEEKS[2] : CHIP_DATES[5];
  return { instrument: chipInstrumentFixture, product, frequency,
    from: "2025-12-23", to: "2026-01-23", items, count: items.length,
    next_cursor: null, read_at: AT, freshness: freshness(frequency, last),
    coverage: coverage(first, last, items.length), source_contract_versions: ["SYNTHETIC-chip-v1"] };
}
function calc<T extends Record<string, unknown> = Record<never, never>>(value: string | null, extra: T = {} as T) {
  return { value, status: value === null ? "missing_data" : "complete",
    formula: "SYNTHETIC fixed fixture result; no frontend calculation", algorithm_version: "SYNTHETIC-calculator-v1",
    reasons: value === null ? ["SYNTHETIC missing input"] : [], quantity_unit: "shares", input_refs: [], ...extra };
}
function institutionalInput(day: string) {
  return { instrument_id: chipInstrumentFixture.id, market_id: chipInstrumentFixture.market.id,
    trade_date: day, quantity_unit: "shares", report_completeness: "complete", scope: chipScopeFixture,
    lineage: { source_id: chipSourceFixture.id, source_contract_version: "SYNTHETIC-chip-v1",
      parser_version: "SYNTHETIC-parser-v1", revision_id: UUID, run_id: UUID, raw_id: UUID,
      source_row_locator: "SYNTHETIC/row", payload_hash: "a".repeat(64), promotion_policy_version: "SYNTHETIC-promotion-v1" },
    timing: timing(day), taxonomy_version: "SYNTHETIC-taxonomy-v1" };
}
function net(day: string, category: string, value: number, period: number | null = null) {
  return calc(String(value), { instrument_id: chipInstrumentFixture.id, market_id: chipInstrumentFixture.market.id,
    metric: period === null ? "daily_net" : "cumulative_net", category, as_of: day, anchor_date: day,
    period, calendar_version: "SYNTHETIC-confirmed-calendar-v1", session_dates: period === null ? [day] : CHIP_DATES.slice(-period),
    missing_dates: [], missing_components: [], input_refs: [institutionalInput(day)], parameters: period === null ? {} : { period } });
}
function component(category: string, value: number, role = "leaf") {
  return { category, mapping_status: "verified", aggregation_role: role,
    buy_quantity: String(Math.max(value, 0)), sell_quantity: String(Math.max(-value, 0)),
    net_quantity: String(value), unavailable_reasons: [] };
}

export function institutionalHistoryFixture() {
  const values = [100, -20, 0, 50, 30, 20];
  const items = CHIP_DATES.map((trade_date, index) => ({ trade_date,
    trade_scope: chipScopeFixture, timing: timing(trade_date), provenance: chipProvenanceFixture,
    quantity_unit: "shares", taxonomy_version: "SYNTHETIC-taxonomy-v1", quality,
    components: [component("foreign_ex_dealers", values[index]), component("investment_trust", 10),
      component("dealer_proprietary", 7), component("dealer_hedging", -2), component("dealer_total", 5, "subtotal")],
    derived_daily: [net(trade_date, "foreign_ex_dealers", values[index]), net(trade_date, "investment_trust", 10),
      net(trade_date, "dealer_total", 5), net(trade_date, "dealer_proprietary", 7), net(trade_date, "dealer_hedging", -2)] }));
  return { ...base("institutional_daily", "1d", items), trade_scope: chipScopeFixture };
}
export function institutionalMetricsFixture(category = "foreign_ex_dealers") {
  const history = institutionalHistoryFixture();
  const sums: Record<string, number> = { foreign_ex_dealers: 80, investment_trust: 50, dealer_total: 25 };
  return { instrument: chipInstrumentFixture, product: "institutional_daily", frequency: "1d", from: history.from, to: history.to,
    count: history.count, read_at: AT, trade_scope: chipScopeFixture, category,
    daily: net(CHIP_DATES[5], category, category === "foreign_ex_dealers" ? 20 : category === "investment_trust" ? 10 : 5),
    cumulative: [5,10,20,60].map((period) => period === 5 ? net(CHIP_DATES[5], category, sums[category] ?? 25, period) : {
      ...net(CHIP_DATES[5], category, 0, period), value: null, status: "insufficient_history", reasons: ["insufficient_confirmed_sessions"] }),
    streak: { instrument_id: chipInstrumentFixture.id, market_id: chipInstrumentFixture.market.id,
      metric: "consecutive_net", category, as_of: CHIP_DATES[5], anchor_date: CHIP_DATES[5], status: category === "foreign_ex_dealers" ? "complete" : "truncated_history",
      quantity_unit: "shares", buy_count: category === "foreign_ex_dealers" ? 3 : null, sell_count: 0, buy_lower_bound: category === "foreign_ex_dealers" ? 3 : 6, sell_lower_bound: 0,
      left_censored: category !== "foreign_ex_dealers", formula: "SYNTHETIC confirmed consecutive sessions", algorithm_version: "1.0.0",
      calendar_version: "SYNTHETIC-confirmed-calendar-v1", session_dates: CHIP_DATES.slice(-4), missing_dates: [],
      missing_components: [], input_refs: [institutionalInput(CHIP_DATES[5])], reasons: [], parameters: {} },
    calendar: { covered_from: CHIP_DATES[0], covered_to: CHIP_DATES[5], verified: true,
      version: "SYNTHETIC-confirmed-calendar-v1", history_start_verified: false },
    coverage: history.coverage, freshness: history.freshness, source_contract_versions: history.source_contract_versions,
    algorithm_version: "1.0.0", precision: { internal_digits: 50 } };
}

export function ownershipHistoryFixture() {
  const percentages = ["60", "65", "70"];
  const items = CHIP_WEEKS.map((as_of_date, index) => ({ as_of_date, frequency: "1w", timing: timing(as_of_date),
    provenance: chipProvenanceFixture, quantity_unit: "shares", denominator: "depository_inventory",
    bucket_scheme_version: "SYNTHETIC-five-bucket-v1", adjustment_value_convention: "signed_delta",
    source_total_holders: "11", source_total_quantity: "10000000", quality,
    buckets: [
      { code: "small", kind: "regular", label: "SYNTHETIC 1–400000股", lower_inclusive: "1", upper_inclusive: "400000", holder_count: "10", quantity: String((100-Number(percentages[index]))*100000), reported_percent: String(100-Number(percentages[index])), missing: false },
      { code: "middle", kind: "regular", label: "SYNTHETIC 400001–1000000股", lower_inclusive: "400001", upper_inclusive: "1000000", holder_count: "0", quantity: "0", reported_percent: "0", missing: false },
      { code: "large", kind: "regular", label: "SYNTHETIC 1000001股以上", lower_inclusive: "1000001", upper_inclusive: null, holder_count: "1", quantity: String(Number(percentages[index])*100000), reported_percent: percentages[index], missing: false },
      { code: "adjustment", kind: "adjustment", label: "SYNTHETIC 差異調整", lower_inclusive: null, upper_inclusive: null, holder_count: "0", quantity: "0", reported_percent: "0", missing: false },
      { code: "total", kind: "total", label: "SYNTHETIC 合計", lower_inclusive: null, upper_inclusive: null, holder_count: "11", quantity: "10000000", reported_percent: "100", missing: false },
    ] }));
  return base("ownership_weekly", "1w", items);
}
function ownershipResult(day: string, value: string | null, previous: string | null, change = false) {
  return { ...calc(value), metric: change ? "ownership_large_holder_change_pp" : "ownership_large_holder_percent",
    unit: change ? "percentage_points" : "percent", threshold: "400001", threshold_unit: "shares", as_of_date: day,
    previous_as_of_date: previous, numerator_quantity: "7000000", denominator_quantity: "10000000", selected_bucket_codes: ["middle", "large"],
    input_refs: [{ instrument_id: chipInstrumentFixture.id, market_id: chipInstrumentFixture.market.id,
      revision_id: UUID, source_id: chipSourceFixture.id, run_id: UUID, raw_id: UUID, source_contract_version: "SYNTHETIC-chip-v1",
      parser_version: "SYNTHETIC-parser-v1", bucket_scheme_version: "SYNTHETIC-five-bucket-v1", adjustment_value_convention: "signed_delta",
      quantity_unit: "shares", denominator: "depository_inventory", identity_evidence_version: "SYNTHETIC-reviewed-v1", promotion_policy_version: "SYNTHETIC-promotion-v1",
      source_row_locator: "SYNTHETIC/row", normalized_hash: "a".repeat(64), ...timing(day) }],
    schedule_version: "SYNTHETIC-weekly-calendar-v1", missing_dates: [], current_ratio: change ? (ratioByDate[day] ?? null) : value, previous_ratio: previous === null ? null : (ratioByDate[previous] ?? null),
    algorithm_version: "ownership-metrics-1.0.0", precision: { internal_digits: 50, output_decimal_places: 12, rounding: "ROUND_HALF_EVEN" },
    parameters: { threshold: "400001", threshold_unit: "shares" } };
}
export function ownershipMetricsFixture() {
  const items = CHIP_WEEKS.map((as_of_date, index) => ({ as_of_date,
    ratio: ownershipResult(as_of_date, ["60.000000000000", "65.000000000000", "70.000000000000"][index], null),
    change_pp: ownershipResult(as_of_date, index === 0 ? null : "5.000000000000", index === 0 ? null : CHIP_WEEKS[index-1], true) }));
  return { ...base("ownership_weekly", "1w", items), threshold: "400001", threshold_unit: "shares",
    schedule: { covered_from: CHIP_WEEKS[0], covered_to: CHIP_WEEKS[2], verified: true, version: "SYNTHETIC-weekly-calendar-v1" },
    algorithm_version: "ownership-metrics-1.0.0", precision: { internal_digits: 50, output_decimal_places: 12, rounding: "ROUND_HALF_EVEN" } };
}
function quantity(value: string | null, availability = value === null ? "missing" : "reported") {
  return { value, availability, unit: "shares" };
}
function ledger(previous: string, balance: string, change: string) {
  return { previous_balance: quantity(previous), balance: quantity(balance),
    signed_adjustment: quantity(null, "not_applicable"), balance_semantics: "stock", flow_semantics: "daily_flow",
    formula_policy: { version: "SYNTHETIC-formula-v1", verified: true, adjustment_treatment: "none" },
    identity: { ...calc(balance), metric: "SYNTHETIC_balance_identity", expected: balance, reported: balance, difference: "0", missing_fields: [], input_revision_ids: [UUID], input_trade_dates: [CHIP_DATES[5]], decimal_precision: 50 },
    daily_change: { ...calc(change), metric: "SYNTHETIC_balance_change", expected: null, reported: null, difference: null, missing_fields: [], input_revision_ids: [UUID], input_trade_dates: [CHIP_DATES[5]], decimal_precision: 50 },
    quota_limit: null, available_quota: { ...calc(null), metric: "SYNTHETIC_available_quota", expected: null, reported: null, difference: null, missing_fields: ["quota_limit"], input_revision_ids: [UUID], input_trade_dates: [CHIP_DATES[5]], decimal_precision: 50 } };
}
export function marginHistoryFixture() {
  const items = CHIP_DATES.map((trade_date, index) => ({ trade_date, balance_scope: "margin_trading",
    quantity_unit: "shares", source_quantity_unit: "shares", timing: timing(trade_date), provenance: chipProvenanceFixture, quality,
    financing: { ...ledger(String(1000000+index*10000), String(1010000+index*10000), "10000"), purchase: quantity("20000"), sale: quantity("10000"), cash_repayment: quantity("0") },
    short_margin: { ...ledger(String(100000+index*1000), String(101000+index*1000), "1000"), short_sell: quantity("2000"), short_cover: quantity("1000"), stock_repayment: quantity("0") } }));
  return { ...base("margin_daily", "1d", items), balance_scope: "margin_trading" };
}
export function lendingHistoryFixture(kind: "borrowed_positions" | "lending_short_sales" = "lending_short_sales") {
  const items = CHIP_DATES.map((trade_date, index) => ({ trade_date, report_kind: kind, trade_scope: "unspecified",
    lending_channel: { channel: "unspecified", code: "unmapped", family: "SYNTHETIC-source-family", version: "SYNTHETIC-channel-v1", verified: false, coverage: [] },
    quantity_unit: "shares", timing: timing(trade_date), provenance: chipProvenanceFixture,
    quality: { status: "not_verified", identity_status: "complete", channel_verified: false, trade_scope_verified: false,
      missing_fields: [], unavailable_reasons: ["source_channel_not_verified", "trade_scope_not_verified"] },
    ledger: { ...ledger(String(100000+index*1000), String(101000+index*1000), "1000"),
      ...(kind === "lending_short_sales" ? { short_sales: quantity("2000"), short_returns: quantity("1000") }
        : { borrowed_quantity: quantity("2000"), returned_quantity: quantity("1000") }),
      signed_adjustment: kind === "lending_short_sales" ? quantity("0") : quantity(null, "not_applicable"),
      formula_policy: { version: "SYNTHETIC-formula-v1", verified: true, adjustment_treatment: kind === "lending_short_sales" ? "signed" : "none" } },
    quotas: [] }));
  return { ...base("lending_daily", "1d", items), report_kind: kind, trade_scope: "unspecified",
    lending_channel_filter: null, channel_family_filter: null, aggregation: "none" };
}

export const chipLatestFixture = { ...latestFixture, instrument: chipInstrumentFixture,
  bar: { ...barFixture, trade_date: CHIP_DATES[5], period_start: CHIP_DATES[5], period_end: CHIP_DATES[5], first_trade_date: CHIP_DATES[5], last_trade_date: CHIP_DATES[5], last_ingested_at: AT, open: "17", high: "18", low: "16", close: "17", turnover: "209865", sources: [{ ...chipSourceFixture, source_type: "manual" }] },
  freshness: { ...latestFixture.freshness, market_local_date: "2026-01-23", latest_trade_date: CHIP_DATES[5], age_calendar_days: 4, as_of: AT },
  history: { available_from: CHIP_DATES[0], available_to: CHIP_DATES[5], completeness: "not_verified" as const } };
export const chipBarsFixture = { ...barsFixture, instrument: chipInstrumentFixture, freshness: chipLatestFixture.freshness,
  history: chipLatestFixture.history, from: CHIP_DATES[0], to: "2026-01-23", count: CHIP_DATES.length,
  bars: CHIP_DATES.map((day,index) => ({ ...barFixture, trade_date: day, period_start: day, period_end: day, first_trade_date: day, last_trade_date: day,
    close: String(12+index), open: String(12+index), high: String(13+index), low: String(11+index), turnover: String((12+index)*12345), calendar_missing_dates: [day], last_ingested_at: AT, sources: [{ ...chipSourceFixture, source_type: "manual" }] })) };
