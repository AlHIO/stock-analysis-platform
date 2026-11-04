/** Synthetic indicator responses only for tests; never imported by production. */
import type {
  IndicatorName,
  IndicatorResponse,
} from "../../src/lib/technical-types";
import { latestFixture } from "./market-api";

export function indicatorFixture(
  indicator: IndicatorName = "sma",
  patch: Partial<IndicatorResponse> = {},
): IndicatorResponse {
  const { bar: _bar, ...envelope } = latestFixture;
  return {
    ...envelope,
    timeframe: "1d",
    indicator,
    algorithm_version: "1.0.0",
    price_basis: "raw",
    field: indicator === "volume_sma" ? "volume_shares" : "close",
    parameters:
      indicator === "macd"
        ? { fast_period: 12, slow_period: 26, signal_period: 9 }
        : { period: indicator === "rsi" ? 14 : 20 },
    required_samples: indicator === "macd" ? 34 : indicator === "rsi" ? 15 : 20,
    from: "2026-01-05",
    to: "2026-01-05",
    count: 1,
    points: [
      {
        trade_date: "2026-01-05",
        values:
          indicator === "macd"
            ? {
                macd: "1.200000000000",
                signal: "1.000000000000",
                histogram: "0.200000000000",
              }
            : { [indicator]: "12.500000000000" },
        status: "ready",
        is_provisional: false,
      },
    ],
    precision: {
      internal_digits: 50,
      output_decimal_places: 12,
      rounding: "ROUND_HALF_EVEN",
    },
    calculation: {
      seed_policy: "full_available_history",
      history_from: "2025-12-01",
      input_count: 20,
      prehistory_count: 19,
      missing_policy: "reset_on_explicit_missing",
      calendar_policy: "observed_sessions_with_known_gaps",
      history_completeness: "not_verified",
    },
    ...patch,
  };
}
