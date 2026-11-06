import { describe, expect, it } from "vitest";
import {
  alignedHistoryRange,
  presetHistoryRange,
  recommendedHistoryTimeframe,
  validateHistoryRange,
} from "../src/lib/history-range";

describe("history query ranges", () => {
  it.each([
    ["1y", "2025-11-05"],
    ["5y", "2021-11-05"],
    ["10y", "2016-11-05"],
  ] as const)("anchors %s to the actual saved last date", (preset, from) => {
    expect(presetHistoryRange("2000-01-01", "2026-11-05", preset)).toEqual({
      from,
      to: "2026-11-05",
    });
  });

  it.each([
    ["1y", "2023-02-28"],
    ["5y", "2019-02-28"],
    ["10y", "2014-02-28"],
  ] as const)("clips leap-day %s presets to a real calendar date", (preset, from) => {
    expect(presetHistoryRange(null, "2024-02-29", preset)).toEqual({
      from,
      to: "2024-02-29",
    });
  });

  it("clamps to the first saved date without claiming the intervening dates exist", () => {
    expect(presetHistoryRange("2020-07-03", "2026-11-05", "10y")).toEqual({
      from: "2020-07-03",
      to: "2026-11-05",
    });
    expect(presetHistoryRange("2026-11-05", "2026-11-05", "10y")).toEqual({
      from: "2026-11-05",
      to: "2026-11-05",
    });
  });

  it.each([
    [null, null],
    ["2026-01-02", "2026-01-01"],
    ["invalid", "2026-01-01"],
    ["2020-01-01", "2026-02-30"],
  ])("does not create a preset from invalid coverage %s/%s", (from, to) => {
    expect(presetHistoryRange(from, to, "10y")).toBeUndefined();
  });

  it.each([
    "",
    "2026-2-03",
    "2026-02-29",
    "2024-02-30",
    "2026-13-01",
    "2026-01-00",
    "0000-01-01",
    "2026-01-01T00:00:00Z",
  ])("rejects malformed or impossible date %s", (from) => {
    expect(validateHistoryRange({ from, to: "2026-12-31" })).toBe(
      "請輸入有效的起訖日期。",
    );
  });

  it("accepts leap days and same-day ranges but rejects reversed dates", () => {
    expect(validateHistoryRange({ from: "2024-02-29", to: "2024-02-29" })).toBeNull();
    expect(validateHistoryRange({ from: "2026-11-06", to: "2026-11-05" })).toBe(
      "開始日期不可晚於結束日期。",
    );
  });

  it("counts the inclusive API limit exactly", () => {
    const start = new Date("2016-11-05T00:00:00Z");
    const end = new Date(start);
    end.setUTCDate(end.getUTCDate() + 3659);
    const allowed = { from: "2016-11-05", to: end.toISOString().slice(0, 10) };
    expect(validateHistoryRange(allowed)).toBeNull();
    end.setUTCDate(end.getUTCDate() + 1);
    expect(validateHistoryRange({ ...allowed, to: end.toISOString().slice(0, 10) })).toContain(
      "3,660",
    );
  });

  it("recommends weekly output above 1000 calendar days, even when history has gaps", () => {
    const end = new Date("2020-01-01T00:00:00Z");
    end.setUTCDate(end.getUTCDate() + 999);
    expect(recommendedHistoryTimeframe({ from: "2020-01-01", to: end.toISOString().slice(0, 10) })).toBe("1d");
    end.setUTCDate(end.getUTCDate() + 1);
    expect(recommendedHistoryTimeframe({ from: "2020-01-01", to: end.toISOString().slice(0, 10) })).toBe("1w");
    expect(recommendedHistoryTimeframe({ from: "invalid", to: "2026-11-05" })).toBe("1d");
  });

  it("aligns normal weekly and monthly requests without changing the selected end", () => {
    const range = { from: "2026-01-02", to: "2026-02-03" };
    expect(alignedHistoryRange(range, "1d")).toEqual(range);
    expect(alignedHistoryRange(range, "1w")).toEqual({ ...range, from: "2025-12-29" });
    expect(alignedHistoryRange(range, "1M")).toEqual({ ...range, from: "2026-01-01" });
    expect(range.from).toBe("2026-01-02");
  });

  it("rejects bucket alignment exceeding the 3660-day contract rather than starting mid-bucket", () => {
    const range = presetHistoryRange(null, "2026-11-29", "10y")!;
    expect(alignedHistoryRange(range, "1M")).toBeUndefined();
    expect(validateHistoryRange(range)).toBeNull();
    expect(alignedHistoryRange(range, "1d")).toEqual(range);
    const supported = alignedHistoryRange({ from: "2016-11-05", to: "2026-11-05" }, "1M");
    expect(supported).toEqual({ from: "2016-11-01", to: "2026-11-05" });
  });

  it("does not send invalid or unavailable ranges into an aligned query", () => {
    expect(alignedHistoryRange(undefined, "1w")).toBeUndefined();
    expect(alignedHistoryRange({ from: "2026-02-30", to: "2026-12-01" }, "1M")).toBeUndefined();
  });
});
