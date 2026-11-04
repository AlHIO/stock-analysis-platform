import { describe, expect, it } from "vitest";
import {
  chartPricePrecision,
  chartTimeKey,
  comparePrices,
  displayPrice,
  toChartData,
} from "../src/lib/chart-data";
import type { Bar } from "../src/lib/market-types";

// Synthetic bars are test-only fixtures, never served as production market data.
export function testBar(overrides: Partial<Bar> = {}): Bar {
  return {
    trade_date: "2026-01-09",
    period_start: "2026-01-09",
    period_end: "2026-01-09",
    first_trade_date: "2026-01-09",
    last_trade_date: "2026-01-09",
    open: "100.00000000",
    high: "105.50000000",
    low: "98.00000000",
    close: "104.00000000",
    volume_shares: 123456,
    turnover: null,
    trade_count: null,
    observed_session_count: 1,
    expected_session_count: null,
    period_closed: true,
    completeness: "unknown",
    is_complete: false,
    completeness_reasons: ["calendar_unknown"],
    missing_trade_dates: [],
    calendar_missing_dates: [],
    sources: [],
    last_ingested_at: "2026-01-09T08:00:00Z",
    previous_observed: null,
    previous_session_verified: null,
    reference_price: null,
    change: null,
    change_percent: null,
    change_unavailable_reason: "official_reference_not_available",
    ...overrides,
  };
}

describe("chart conversion", () => {
  it("keeps observed local trade dates and does not add weekends or missing sessions", () => {
    const friday = testBar();
    const monday = testBar({ trade_date: "2026-01-12" });
    const original = [monday, friday];
    const data = toChartData(original);
    expect(data.candles.map((c) => c.time)).toEqual([
      "2026-01-09",
      "2026-01-12",
    ]);
    expect(original[0]).toBe(monday);
    expect(data.candles[0]).toEqual({
      time: "2026-01-09",
      open: 100,
      high: 105.5,
      low: 98,
      close: 104,
    });
    expect(data.volume[0].value).toBe(123456);
    expect(data.barsByDate.get("2026-01-09")).toBe(friday);
    expect(friday.close).toBe("104.00000000");
  });
  it("uses last actual observed date of a backend aggregate rather than Sunday or month end", () => {
    const data = toChartData([
      testBar({
        period_start: "2026-01-05",
        period_end: "2026-01-11",
        observed_session_count: 3,
      }),
    ]);
    expect(data.candles[0].time).toBe("2026-01-09");
    expect(data.barsByDate.get("2026-01-09")?.observed_session_count).toBe(3);
  });
  it("returns genuinely empty chart data for empty history", () => {
    expect(toChartData([])).toEqual({
      candles: [],
      volume: [],
      barsByDate: new Map(),
    });
  });
  it("rejects duplicate observed trade dates", () => {
    expect(() => toChartData([testBar(), testBar()])).toThrow("無法安全繪製");
  });
  it.each([
    { trade_date: "2026-02-30" },
    { trade_date: "2026-1-09" },
    { high: "NaN" },
    { high: "Infinity" },
    { open: "-1" },
    { close: "0" },
    { high: "99" },
    { low: "106" },
    { close: "110" },
    { volume_shares: -1 },
    { volume_shares: 0.5 },
    { volume_shares: Number.MAX_SAFE_INTEGER + 1 },
    { high: "999999999999999999999999999999" },
  ])("rejects invalid or unsafe values: %j", (invalid) => {
    expect(() => toChartData([testBar(invalid)])).toThrow("無法安全繪製");
  });
  it("retains a genuine zero volume", () => {
    expect(toChartData([testBar({ volume_shares: 0 })]).volume[0].value).toBe(
      0,
    );
  });
  it("checks OHLC with decimal strings before chart number conversion", () => {
    expect(() =>
      toChartData([
        testBar({
          open: "100.00000002",
          high: "100.00000001",
          low: "99",
          close: "100",
        }),
      ]),
    ).toThrow();
    expect(
      comparePrices("1000000000000000.00000001", "1000000000000000.00000002"),
    ).toBe(-1);
    expect(comparePrices("1.0000", "1")).toBe(0);
    expect(comparePrices("99.99", "100")).toBe(-1);
  });
  it("rejects a genuine price range that double precision would flatten", () => {
    expect(() =>
      toChartData([
        testBar({
          open: "1000000000000000.00000001",
          high: "1000000000000000.00000002",
          low: "1000000000000000",
          close: "1000000000000000",
        }),
      ]),
    ).toThrow();
  });
  it("rejects distinct exact prices across bars when chart numbers would collapse them", () => {
    const first = "100000000.00000001";
    const second = "100000000.00000002";
    expect(Number(first)).toBe(Number(second));
    const flat = (price: string, date: string) =>
      testBar({
        trade_date: date,
        open: price,
        high: price,
        low: price,
        close: price,
      });
    expect(() =>
      toChartData([flat(first, "2026-01-09"), flat(second, "2026-01-12")]),
    ).toThrow("無法安全繪製");
    expect(
      toChartData([
        flat("1E+2", "2026-01-09"),
        flat("100.00000000", "2026-01-12"),
      ]).candles,
    ).toHaveLength(2);
  });
  it("formats the original decimal string without numerical rounding", () => {
    expect(displayPrice("1234567890123456.12345678")).toBe(
      "1,234,567,890,123,456.12345678",
    );
    expect(displayPrice("1234.50000000")).toBe("1,234.5");
    expect(displayPrice("0.00000000")).toBe("0");
  });
  it("reads crosshair business-day values without a timezone conversion", () => {
    expect(chartTimeKey("2026-01-09")).toBe("2026-01-09");
    expect(chartTimeKey({ year: 2026, month: 1, day: 9 })).toBe("2026-01-09");
  });
  it("accepts Pydantic scientific-notation prices without losing their decimal scale", () => {
    const source = testBar({
      open: "2E-8",
      high: "4E-8",
      low: "1E-8",
      close: "3E-8",
    });
    const data = toChartData([source]);
    expect(data.candles[0]).toMatchObject({
      open: 0.00000002,
      high: 0.00000004,
      low: 0.00000001,
      close: 0.00000003,
    });
    expect(data.barsByDate.get(source.trade_date)?.close).toBe("3E-8");
    expect(displayPrice(source.close)).toBe("0.00000003");
    expect(chartPricePrecision([source])).toBe(8);
    expect(comparePrices("1E-8", "0.00000002")).toBe(-1);
    expect(comparePrices("1E+2", "100.00000000")).toBe(0);
  });
  it("rejects zero and invalid exponent OHLC without producing a chart", () => {
    expect(() => toChartData([testBar({ low: "0E-8" })])).toThrow();
    expect(() => toChartData([testBar({ high: "1E99999999" })])).toThrow();
    expect(displayPrice("0E-8")).toBe("0");
    expect(displayPrice("-1.25E+3")).toBe("-1,250");
    expect(displayPrice("1E-99999999")).toBe("—");
  });
});
