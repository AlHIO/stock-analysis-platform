import { describe, expect, it } from "vitest";
import { analyzeHistoryGaps } from "../src/lib/history-gaps";
import type { Bar } from "../src/lib/market-types";
import { barFixture } from "./fixtures/market-api";

/** Synthetic observations only; these prices never enter production data. */
function observation(first: string, last = first): Bar {
  return {
    ...barFixture,
    trade_date: last,
    first_trade_date: first,
    last_trade_date: last,
    period_start: first,
    period_end: last,
  };
}

describe("observed historical date intervals", () => {
  it("identifies the real-date shape of an isolated 2016 observation and recent 2026 rows", () => {
    const bars = [
      observation("2016-11-07"),
      observation("2026-08-25"),
      observation("2026-08-26"),
      observation("2026-08-27"),
    ];
    const before = JSON.stringify(bars);
    expect(analyzeHistoryGaps(bars)).toEqual({
      gaps: [
        {
          previousDate: "2016-11-07",
          nextDate: "2026-08-25",
          calendarDays: 3578,
        },
      ],
      recentFrom: "2026-08-25",
    });
    expect(JSON.stringify(bars)).toBe(before);
    expect(bars).toHaveLength(4);
  });

  it("does not interpret a weekend or a long holiday-sized interval as missing sessions", () => {
    expect(
      analyzeHistoryGaps([
        observation("2026-01-09"),
        observation("2026-01-12"),
        observation("2026-01-26"),
      ]),
    ).toEqual({ gaps: [], recentFrom: "2026-01-09" });
  });

  it("uses a strictly greater-than-90 calendar-day attention threshold", () => {
    expect(
      analyzeHistoryGaps([observation("2020-01-01"), observation("2020-03-31")])
        .gaps,
    ).toEqual([]);
    expect(
      analyzeHistoryGaps([observation("2020-01-01"), observation("2020-04-01")])
        .gaps,
    ).toEqual([
      {
        previousDate: "2020-01-01",
        nextDate: "2020-04-01",
        calendarDays: 91,
      },
    ]);
  });

  it("returns the start following the final long interval when several exist", () => {
    const result = analyzeHistoryGaps([
      observation("2016-11-07"),
      observation("2020-01-02"),
      observation("2020-01-03"),
      observation("2026-08-25"),
      observation("2026-08-26"),
    ]);
    expect(result.gaps).toHaveLength(2);
    expect(result.gaps.map((gap) => gap.nextDate)).toEqual([
      "2020-01-02",
      "2026-08-25",
    ]);
    expect(result.recentFrom).toBe("2026-08-25");
  });

  it("compares the actual observations of weekly aggregates rather than Sunday or Monday", () => {
    const first = observation("2016-11-07", "2016-11-11");
    first.period_end = "2016-11-13";
    const second = observation("2026-08-25", "2026-08-28");
    second.period_start = "2026-08-24";
    second.period_end = "2026-08-30";
    expect(analyzeHistoryGaps([first, second]).gaps).toEqual([
      {
        previousDate: "2016-11-11",
        nextDate: "2026-08-25",
        calendarDays: 3574,
      },
    ]);
  });

  it("does not let a monthly calendar envelope hide an interval in the actual observations", () => {
    const first = observation("2020-01-02", "2020-01-03");
    first.period_end = "2020-01-31";
    const second = observation("2020-04-06", "2020-04-30");
    second.period_start = "2020-04-01";
    expect(analyzeHistoryGaps([first, second])).toEqual({
      gaps: [
        {
          previousDate: "2020-01-03",
          nextDate: "2020-04-06",
          calendarDays: 94,
        },
      ],
      recentFrom: "2020-04-06",
    });
  });

  it("sorts a copy without mutating caller order", () => {
    const first = observation("2016-11-07");
    const recent = observation("2026-08-25");
    const bars = Object.freeze([recent, first]);
    expect(analyzeHistoryGaps(bars).recentFrom).toBe("2026-08-25");
    expect(bars[0]).toBe(recent);
    expect(bars[1]).toBe(first);
  });

  it("handles no observations and one partial aggregate without implying completeness", () => {
    expect(analyzeHistoryGaps([])).toEqual({ gaps: [], recentFrom: null });
    expect(
      analyzeHistoryGaps([observation("2026-08-25", "2026-08-28")]),
    ).toEqual({ gaps: [], recentFrom: "2026-08-25" });
  });

  it.each([
    { first_trade_date: "2026-02-30" },
    { last_trade_date: "2026-02-30" },
    { trade_date: "2026-02-30" },
    { first_trade_date: "2026-1-05" },
    { first_trade_date: "0000-01-01" },
    { first_trade_date: "2026-01-06" },
    { trade_date: "2026-01-06" },
  ])(
    "rejects malformed or internally inconsistent observed dates: %j",
    (changes) => {
      expect(() =>
        analyzeHistoryGaps([{ ...observation("2026-01-05"), ...changes }]),
      ).toThrow(new RangeError("Invalid observed history dates"));
    },
  );

  it("rejects duplicate or overlapping observed intervals", () => {
    expect(() =>
      analyzeHistoryGaps([
        observation("2026-01-05"),
        observation("2026-01-05"),
      ]),
    ).toThrow(RangeError);
    expect(() =>
      analyzeHistoryGaps([
        observation("2026-01-05", "2026-01-09"),
        observation("2026-01-09", "2026-01-16"),
      ]),
    ).toThrow(RangeError);
  });
});
