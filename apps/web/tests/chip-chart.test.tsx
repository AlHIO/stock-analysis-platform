/** SYNTHETIC chart/lifecycle tests, no market or provider requests. */
import React from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChipChart, type ChipSeries } from "../src/components/chip-chart";

const mock = vi.hoisted(() => ({
  create: vi.fn(), add: vi.fn(), removeSeries: vi.fn(), remove: vi.fn(), resize: vi.fn(),
  crosshair: vi.fn(), uncrosshair: vi.fn(), rangeChange: vi.fn(), unrangeChange: vi.fn(),
  getRange: vi.fn(), setRange: vi.fn(), getTime: vi.fn(), setTime: vi.fn(), fit: vi.fn(),
  observe: vi.fn(), disconnect: vi.fn(), series: [] as { setData: ReturnType<typeof vi.fn> }[],
}));

vi.mock("lightweight-charts", () => ({
  createChart: mock.create, ColorType: { Solid: "solid" }, CrosshairMode: { Normal: 0 },
  HistogramSeries: { type: "histogram" }, LineSeries: { type: "line" },
}));

const points = [
  { date: "2026-01-02", value: "60.123456789012" },
  { date: "2026-01-09", value: "70.234567890123" },
];
const series: ChipSeries[] = [{ id: "SYNTHETIC", label: "合成測試比例", color: "#61d5be", kind: "line", points }];
let resizeCallback: ResizeObserverCallback;

beforeEach(() => {
  vi.clearAllMocks();
  mock.series.length = 0;
  sessionStorage.clear();
  mock.getRange.mockReturnValue(null);
  mock.getTime.mockReturnValue(null);
  mock.add.mockImplementation(() => {
    const value = { setData: vi.fn() };
    mock.series.push(value);
    return value;
  });
  mock.create.mockReturnValue({
    addSeries: mock.add, removeSeries: mock.removeSeries, remove: mock.remove, resize: mock.resize,
    subscribeCrosshairMove: mock.crosshair, unsubscribeCrosshairMove: mock.uncrosshair,
    timeScale: () => ({ fitContent: mock.fit, getVisibleLogicalRange: mock.getRange,
      setVisibleLogicalRange: mock.setRange, getVisibleRange: mock.getTime, setVisibleRange: mock.setTime,
      subscribeVisibleLogicalRangeChange: mock.rangeChange, unsubscribeVisibleLogicalRangeChange: mock.unrangeChange }),
  });
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
    observe = mock.observe; disconnect = mock.disconnect;
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(285);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("ChipChart", () => {
  it("renders real source weekly dates, accessible unit and exact crosshair values", async () => {
    render(<ChipChart chartId="synthetic-weekly" title="測試週比例" unit="%" frequency="1w" series={series} />);
    await waitFor(() => expect(mock.add).toHaveBeenCalled());
    expect(mock.series[0].setData).toHaveBeenCalledWith([
      { time: "2026-01-02", value: 60.123456789012 }, { time: "2026-01-09", value: 70.234567890123 },
    ]);
    expect(screen.getByRole("img", { name: "測試週比例，每週統計，單位%" })).toHaveAttribute("data-chart-ready", "true");
    expect(screen.getByText("70.234567890123")).toBeInTheDocument();
    const handler = mock.crosshair.mock.calls[0][0];
    act(() => handler({ time: { year: 2026, month: 1, day: 2 }, point: { x: 20, y: 20 } }));
    expect(within(screen.getByRole("tooltip")).getByText(/60.123456789012/)).toBeInTheDocument();
    expect(screen.getByText(/不建立每日持股點/)).toBeInTheDocument();
  });

  it("separates line gaps instead of drawing through null observations", async () => {
    const values: ChipSeries[] = [{ ...series[0], points: [
      points[0], { date: "2026-01-05", value: null }, points[1],
    ] }];
    render(<ChipChart chartId="gap" title="缺值圖" unit="股" frequency="1d" series={values} />);
    await waitFor(() => expect(mock.series).toHaveLength(3));
    expect(mock.series[0].setData).toHaveBeenCalledWith([{ time: "2026-01-02", value: 60.123456789012 }]);
    expect(mock.series[1].setData).toHaveBeenCalledWith([{ time: "2026-01-09", value: 70.234567890123 }]);
    expect(mock.series[2].setData).toHaveBeenCalledWith([{ time: "2026-01-05" }]);
  });

  it("retains chart instance and user's viewport across same-period data updates", async () => {
    const view = render(<ChipChart chartId="keep-view" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(mock.fit).toHaveBeenCalledTimes(1));
    mock.getRange.mockReturnValue({ from: 0.25, to: 1.25 });
    view.rerender(<ChipChart chartId="keep-view" title="籌碼" unit="股" frequency="1d" series={[
      { ...series[0], points: [points[0], { ...points[1], value: "71" }] },
    ]} />);
    await waitFor(() => expect(mock.setRange).toHaveBeenCalledWith({ from: 0.25, to: 1.25 }));
    expect(mock.create).toHaveBeenCalledTimes(1);
    expect(mock.fit).toHaveBeenCalledTimes(1);
    expect(mock.removeSeries).toHaveBeenCalledTimes(1);
  });

  it("preserves date overlap when changing input extent and fits disjoint replacement ranges", async () => {
    const view = render(<ChipChart chartId="ranges" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(mock.fit).toHaveBeenCalledTimes(1));
    mock.getTime.mockReturnValue({ from: "2026-01-02", to: "2026-01-09" });
    view.rerender(<ChipChart chartId="ranges" title="籌碼" unit="股" frequency="1d" series={[
      { ...series[0], points: [...points, { date: "2026-01-12", value: "72" }] },
    ]} />);
    await waitFor(() => expect(mock.setTime).toHaveBeenCalledWith({ from: "2026-01-02", to: "2026-01-09" }));
    expect(mock.fit).toHaveBeenCalledTimes(1);
    view.rerender(<ChipChart chartId="ranges" title="籌碼" unit="股" frequency="1d" series={[
      { ...series[0], points: [{ date: "2026-02-02", value: "73" }] },
    ]} />);
    await waitFor(() => expect(mock.fit).toHaveBeenCalledTimes(2));
    expect(mock.create).toHaveBeenCalledTimes(1);
  });

  it("ignores hidden-panel zero sizes and resizes when shown without refitting", async () => {
    render(<ChipChart chartId="resize" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(mock.fit).toHaveBeenCalledTimes(1));
    act(() => resizeCallback([{ contentRect: { width: 0, height: 0 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(mock.resize).not.toHaveBeenCalled();
    act(() => resizeCallback([{ contentRect: { width: 360, height: 245 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(mock.resize).toHaveBeenCalledWith(360, 245);
    expect(mock.fit).toHaveBeenCalledTimes(1);
  });

  it("releases every subscription, observer and chart on unmount and restores a saved view", async () => {
    const view = render(<ChipChart chartId="saved" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(mock.add).toHaveBeenCalled());
    mock.getRange.mockReturnValue({ from: -.25, to: 1.75 });
    view.unmount();
    expect(mock.remove).toHaveBeenCalledTimes(1);
    expect(mock.disconnect).toHaveBeenCalledTimes(1);
    expect(mock.uncrosshair).toHaveBeenCalledWith(mock.crosshair.mock.calls[0][0]);
    expect(mock.unrangeChange).toHaveBeenCalledWith(mock.rangeChange.mock.calls[0][0]);
    mock.getRange.mockReturnValue(null);
    render(<ChipChart chartId="saved" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(mock.setRange).toHaveBeenCalledWith({ from: -.25, to: 1.75 }));
  });

  it("keeps null-only and empty data empty without creating a zero chart", () => {
    render(<ChipChart chartId="empty" title="空資料" unit="股" frequency="1w" series={[
      { ...series[0], points: [{ date: "2026-01-09", value: null }] },
    ]} />);
    expect(screen.getByText(/此區間沒有可繪製/)).toBeInTheDocument();
    expect(mock.create).not.toHaveBeenCalled();
  });

  it("rejects unsafe source values without inventing rendered data", () => {
    render(<ChipChart chartId="invalid" title="不合法" unit="股" frequency="1d" series={[
      { ...series[0], points: [{ date: "2026-01-09", value: "9007199254740992" }] },
    ]} />);
    expect(screen.getByRole("alert")).toHaveTextContent("無法安全繪圖");
    expect(mock.create).not.toHaveBeenCalled();
  });

  it("reports chart initialization failure without losing the exact legend", async () => {
    mock.create.mockImplementation(() => { throw new Error("SYNTHETIC canvas failure"); });
    render(<ChipChart chartId="failed" title="籌碼" unit="股" frequency="1d" series={series} />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("圖表暫時無法顯示"));
    expect(screen.getByText("70.234567890123")).toBeInTheDocument();
  });
});
