/** SYNTHETIC financial observations for offline transformation tests only. */
import { describe, expect, it } from "vitest";
import { ChipChartDataError, toChipChartData, type ChipDatum, type ChipSeries } from "../src/lib/chip-chart";

const line = (points: readonly ChipDatum[], kind: ChipSeries["kind"] = "line"): ChipSeries => ({
  id: "SYNTHETIC-flow", label: "合成測試值", color: "#fa7b85", kind, points,
});

describe("chip chart transformation", () => {
  it("preserves exact source strings and real signed/zero values", () => {
    const points = [
      { date: "2026-01-02", value: "-1234.000000000000" },
      { date: "2026-01-05", value: "0.000000000000" },
      { date: "2026-01-06", value: "87.009580453709" },
    ];
    const [data] = toChipChartData([line(points)]);
    expect(data.data).toEqual([
      { time: "2026-01-02", value: -1234 },
      { time: "2026-01-05", value: 0 },
      { time: "2026-01-06", value: 87.009580453709 },
    ]);
    expect(data.exactByDate.get("2026-01-06")?.value).toBe("87.009580453709");
    expect(data.source.points).toBe(points);
    expect(data.precision).toBe(6);
  });

  it("retains weekly statistics dates without inventing daily or missing-week observations", () => {
    const [data] = toChipChartData([line([
      { date: "2025-12-26", value: "60.123456789012" },
      { date: "2026-01-09", value: "65.234567890123" },
    ])]);
    expect(data.data.map((p) => p.time)).toEqual(["2025-12-26", "2026-01-09"]);
    expect(data.exactByDate.size).toBe(2);
  });

  it("turns explicit null into whitespace and splits connected line segments", () => {
    const [data] = toChipChartData([line([
      { date: "2026-01-02", value: "12" },
      { date: "2026-01-05", value: null, status: "missing_data" },
      { date: "2026-01-06", value: "18" },
    ])]);
    expect(data.data).toEqual([
      { time: "2026-01-02", value: 12 },
      { time: "2026-01-05" },
      { time: "2026-01-06", value: 18 },
    ]);
    expect(data.segments).toEqual([
      [{ time: "2026-01-02", value: 12 }],
      [{ time: "2026-01-06", value: 18 }],
    ]);
    expect(data.data[1]).not.toHaveProperty("value");
  });

  it("keeps histogram gaps and negative flows without treating missing as zero", () => {
    const [data] = toChipChartData([line([
      { date: "2026-01-02", value: "5" },
      { date: "2026-01-05", value: null },
      { date: "2026-01-06", value: "-2" },
    ], "histogram")]);
    expect(data.data).toEqual([
      { time: "2026-01-02", value: 5, color: "#fa7b85" },
      { time: "2026-01-05" },
      { time: "2026-01-06", value: -2, color: "#58c9a6" },
    ]);
  });

  it.each(["9007199254740992", "-9007199254740992", "9007199254740991.01", "NaN", "Infinity", "--", "", "1e400", "1e-400", "0x11"])("rejects unsafe or invalid values %s", (value) => {
    expect(() => toChipChartData([line([{ date: "2026-01-02", value }])])).toThrow(ChipChartDataError);
  });

  it("accepts the exact safe integer limit without a guessed unit conversion", () => {
    const [data] = toChipChartData([line([{ date: "2026-01-02", value: "9007199254740991" }])]);
    expect(data.data[0]).toEqual({ time: "2026-01-02", value: Number.MAX_SAFE_INTEGER });
  });

  it("rejects distinct decimals that collapse to the same pixel number", () => {
    expect(() => toChipChartData([line([
      { date: "2026-01-02", value: "1.000000000000000001" },
      { date: "2026-01-05", value: "1.000000000000000002" },
    ])])).toThrow(ChipChartDataError);
  });

  it("accepts equivalent decimal encodings and signed zero", () => {
    const [data] = toChipChartData([line([
      { date: "2026-01-02", value: "1.0000" },
      { date: "2026-01-05", value: "1" },
      { date: "2026-01-06", value: "-0.000000000000" },
    ])]);
    expect(data.data).toEqual([
      { time: "2026-01-02", value: 1 },
      { time: "2026-01-05", value: 1 },
      { time: "2026-01-06", value: 0 },
    ]);
  });

  it.each([
    [{ date: "2026-02-30", value: "1" }],
    [{ date: "2026-1-2", value: "1" }],
    [{ date: "2026-01-02", value: "1" }, { date: "2026-01-02", value: "2" }],
    [{ date: "2026-01-05", value: "1" }, { date: "2026-01-02", value: "2" }],
  ].map((points) => ({ points })))("rejects invalid, duplicate or unsorted source dates", ({ points }) => {
    expect(() => toChipChartData([line(points)])).toThrow(ChipChartDataError);
  });

  it("rejects duplicate series identity and allows empty data", () => {
    expect(toChipChartData([])).toEqual([]);
    expect(toChipChartData([line([])])[0].segments).toEqual([]);
    expect(() => toChipChartData([line([]), line([])])).toThrow(ChipChartDataError);
  });
});
