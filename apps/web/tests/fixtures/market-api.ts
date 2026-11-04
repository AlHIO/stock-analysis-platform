/** Synthetic fixtures for tests only. Never imported by production components. */
import type { Bar, BarsResponse, Instrument, LatestResponse } from "../../src/lib/market-types";

export const instrumentFixture: Instrument = {
  id: "00000000-0000-0000-0000-000000000001", symbol: "TEST", name: "測試公司",
  market: { id: "00000000-0000-0000-0000-000000000002", code: "TWSE", name: "測試上市市場", timezone: "Asia/Taipei", currency: "TWD" },
  instrument_type: "stock", status: "active", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
export const barFixture: Bar = {
  trade_date: "2026-01-05", period_start: "2026-01-05", period_end: "2026-01-05",
  first_trade_date: "2026-01-05", last_trade_date: "2026-01-05",
  open: "12.10000000", high: "13.00000000", low: "12.00000000", close: "12.50000000",
  volume_shares: 12345, turnover: "154312.50", trade_count: 99,
  observed_session_count: 1, expected_session_count: null, period_closed: true,
  completeness: "unknown", is_complete: false, completeness_reasons: ["calendar_unknown"],
  missing_trade_dates: [], calendar_missing_dates: ["2026-01-05"],
  sources: [{ id: "00000000-0000-0000-0000-000000000003", code: "TEST", name: "測試來源", source_type: "manual" }],
  last_ingested_at: "2026-01-06T00:00:00Z", previous_observed: null, previous_session_verified: null,
  reference_price: null, change: null, change_percent: null, change_unavailable_reason: "official_reference_not_available",
};
export const latestFixture: LatestResponse = {
  instrument: instrumentFixture, bar: barFixture,
  freshness: { as_of: "2026-01-06T00:00:00Z", market_timezone: "Asia/Taipei", market_local_date: "2026-01-06",
    latest_trade_date: "2026-01-05", last_ingested_at: "2026-01-06T00:00:00Z", age_calendar_days: 1,
    is_realtime: false, assessment: "calendar_not_verified" },
  history: { available_from: "2026-01-05", available_to: "2026-01-05", completeness: "not_verified" },
  price_adjustment: "unadjusted", data_kind: "daily_snapshot",
};
export const barsFixture: BarsResponse = {
  instrument: latestFixture.instrument, freshness: latestFixture.freshness, history: latestFixture.history,
  price_adjustment: "unadjusted", data_kind: "daily_snapshot", timeframe: "1d", from: "2025-01-06", to: "2026-01-06",
  count: 1, bars: [barFixture], query_limit: 1000,
};
