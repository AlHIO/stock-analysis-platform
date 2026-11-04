/** Explicitly synthetic server outputs: no indicator formulas are duplicated here. */
import { describe, expect, it } from "vitest";
import type {
  IndicatorName,
  IndicatorResponse,
} from "../src/lib/technical-types";
import {
  indicatorLabel,
  IndicatorChartError,
  toIndicatorChartData,
} from "../src/lib/indicator-chart";
import { barFixture, latestFixture } from "./fixtures/market-api";

const bars = ["2026-01-05", "2026-01-07", "2026-01-09"].map((trade_date) => ({
  ...barFixture,
  trade_date,
}));
function response(
  indicator: IndicatorName,
  values: (string | null)[],
  dates = bars.map((bar) => bar.trade_date),
): IndicatorResponse {
  return {
    ...latestFixture,
    from: dates[0] ?? "2026-01-01",
    to: dates.at(-1) ?? "2026-01-31",
    count: values.length,
    precision: {
      internal_digits: 50,
      output_decimal_places: 12,
      rounding: "ROUND_HALF_EVEN",
    },
    calculation: {
      seed_policy: "full_available_history",
      history_from: dates[0] ?? null,
      input_count: values.length,
      prehistory_count: 0,
      missing_policy: "reset_on_explicit_missing",
      calendar_policy: "observed_sessions_with_known_gaps",
      history_completeness: "not_verified",
    },
    indicator,
    algorithm_version: "1.0.0",
    parameters:
      indicator === "macd"
        ? { fast_period: 2, slow_period: 3, signal_period: 2 }
        : { period: 2 },
    timeframe: "1d",
    price_basis: "raw",
    field: indicator === "volume_sma" ? "volume_shares" : "close",
    required_samples: 2,
    points: values.map((value, index) => ({
      trade_date: dates[index],
      values:
        indicator === "macd"
          ? { macd: value, signal: value, histogram: value }
          : { [indicator]: value },
      status: value === null ? "warmup" : "ready",
      is_provisional: false,
    })),
  };
}

describe("technical chart conversion", () => {
  it("plots exact server values on observed dates without recomputing a formula", () => {
    const result = toIndicatorChartData(
      [response("sma", [null, "123.456789012345", "124.000000000000"])],
      bars,
      "1d",
    )[0];
    expect(result.components[0].data).toEqual([
      { time: "2026-01-05" },
      { time: "2026-01-07", value: 123.456789012345 },
      { time: "2026-01-09", value: 124 },
    ]);
    expect(result.pointsByDate.get("2026-01-07")?.values.sma).toBe(
      "123.456789012345",
    );
    expect(result.components[0].precision).toBe(12);
  });
  it("splits line segments at nulls, rather than bridging a missing-input reset", () => {
    const result = toIndicatorChartData(
      [response("ema", ["10", null, "20"])],
      bars,
      "1d",
    )[0];
    expect(result.components[0].segments).toEqual([
      [{ time: "2026-01-05", value: 10 }],
      [{ time: "2026-01-09", value: 20 }],
    ]);
    expect(result.components[0].data[1]).toEqual({ time: "2026-01-07" });
  });
  it("a known missing session breaks a line without adding a fabricated candle date", () => {
    const indicator = response(
      "ema",
      ["10", null, "20", "30"],
      ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09"],
    );
    const line = toIndicatorChartData([indicator], bars, "1d")[0].components[0];
    expect(line.data.map((point) => point.time)).toEqual(
      bars.map((bar) => bar.trade_date),
    );
    expect(line.segments.map((segment) => segment.length)).toEqual([1, 2]);
  });
  it("ignores values outside the visible candle extent", () => {
    const indicator = response(
      "sma",
      ["99", "10", "20", "30", "100"],
      ["2026-01-02", "2026-01-05", "2026-01-07", "2026-01-09", "2026-01-12"],
    );
    expect(
      toIndicatorChartData([indicator], bars, "1d")[0].components[0].data,
    ).toHaveLength(3);
  });
  it("preserves zero and negative MACD values instead of treating them as missing", () => {
    const components = toIndicatorChartData(
      [response("macd", ["-2.5", "0", "1.5"])],
      bars,
      "1d",
    )[0].components;
    expect(components.map((component) => component.key)).toEqual([
      "macd",
      "signal",
      "histogram",
    ]);
    expect(components[0].data[0]).toEqual({ time: "2026-01-05", value: -2.5 });
    expect(components[0].data[1]).toEqual({ time: "2026-01-07", value: 0 });
    expect(components[2].data[0]).toMatchObject({
      value: -2.5,
      color: "#4bc6a080",
    });
  });
  it("accepts zero volume averages and bounded RSI endpoints", () => {
    expect(
      toIndicatorChartData(
        [response("volume_sma", ["0", "2.5", "3"])],
        bars,
        "1d",
      )[0].components[0].data[0],
    ).toMatchObject({ value: 0 });
    expect(
      toIndicatorChartData([response("rsi", ["0", "50", "100"])], bars, "1d")[0]
        .components[0].segments[0],
    ).toHaveLength(3);
  });
  it("does not create indicators for empty candles or fake zeroes for all warmup data", () => {
    expect(
      toIndicatorChartData([response("rsi", [null, null, null])], [], "1d"),
    ).toEqual([]);
    const line = toIndicatorChartData(
      [response("rsi", [null, null, null])],
      bars,
      "1d",
    )[0].components[0];
    expect(line.segments).toEqual([]);
    expect(line.data.every((point) => !("value" in point))).toBeTruthy();
  });
  it.each(["Infinity", "NaN", "1E+64", "-1", "0"])(
    "rejects invalid or nonpositive overlay value %s",
    (value) => {
      expect(() =>
        toIndicatorChartData(
          [response("sma", [value, null, null])],
          bars,
          "1d",
        ),
      ).toThrow(IndicatorChartError);
    },
  );
  it("rejects unsafe decimal collisions across plotted indicator values", () => {
    expect(() =>
      toIndicatorChartData(
        [response("sma", ["100000000.00000001", "100000000.00000002", null])],
        bars,
        "1d",
      ),
    ).toThrow(IndicatorChartError);
  });
  it("accepts equivalent decimal/exponent values as the same exact number", () => {
    expect(
      toIndicatorChartData(
        [response("sma", ["1E+2", "100.000", null])],
        bars,
        "1d",
      )[0].components[0].segments[0],
    ).toHaveLength(2);
  });
  it("rejects duplicate indicator dates or a stale timeframe/basis", () => {
    expect(() =>
      toIndicatorChartData(
        [
          response(
            "sma",
            ["1", "2", "3"],
            ["2026-01-05", "2026-01-05", "2026-01-09"],
          ),
        ],
        bars,
        "1d",
      ),
    ).toThrow();
    expect(() =>
      toIndicatorChartData([response("sma", ["1", "2", "3"])], bars, "1w"),
    ).toThrow();
    expect(() =>
      toIndicatorChartData(
        [
          {
            ...response("sma", ["1", "2", "3"]),
            price_basis: "adjusted",
          } as unknown as IndicatorResponse,
        ],
        bars,
        "1d",
      ),
    ).toThrow();
  });
  it("rejects duplicate indicators and impossible RSI or volume values", () => {
    const sma = response("sma", ["1", "2", "3"]);
    expect(() => toIndicatorChartData([sma, sma], bars, "1d")).toThrow();
    expect(() =>
      toIndicatorChartData([response("rsi", ["101", null, null])], bars, "1d"),
    ).toThrow();
    expect(() =>
      toIndicatorChartData(
        [response("volume_sma", ["-1", null, null])],
        bars,
        "1d",
      ),
    ).toThrow();
  });
  it("labels the parameters supplied by the server", () => {
    expect(indicatorLabel(response("sma", []))).toBe("SMA(2)");
    expect(indicatorLabel(response("macd", []))).toBe("MACD(2,3,2)");
  });
});
