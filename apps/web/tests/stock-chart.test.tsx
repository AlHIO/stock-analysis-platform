import React from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Bar } from "../src/lib/market-types";
import { StockChart } from "../src/components/stock-chart";

const chartMock = vi.hoisted(() => {
  const series = () => ({ setData: vi.fn() });
  return {
    create: vi.fn(), candles: series(), volume: series(), addSeries: vi.fn(),
    resize: vi.fn(), remove: vi.fn(), subscribe: vi.fn(), unsubscribe: vi.fn(),
    fitContent: vi.fn(), stretch: vi.fn(), disconnect: vi.fn(), observe: vi.fn(),
  };
});
vi.mock("lightweight-charts", () => ({
  createChart: chartMock.create,
  ColorType: { Solid: "solid" }, CrosshairMode: { Normal: 0 },
  CandlestickSeries: { type: "candles" }, HistogramSeries: { type: "volume" },
}));

const bar: Bar = {
  trade_date: "2026-01-09", period_start: "2026-01-05", period_end: "2026-01-11",
  first_trade_date: "2026-01-09", last_trade_date: "2026-01-09",
  open: "100.00000000", high: "105.50000000", low: "98.00000000", close: "104.00000000",
  volume_shares: 123456, turnover: null, trade_count: null, observed_session_count: 1, expected_session_count: null,
  period_closed: true, completeness: "unknown", is_complete: false, completeness_reasons: ["calendar_unknown"],
  missing_trade_dates: [], calendar_missing_dates: [], sources: [], last_ingested_at: "2026-01-09T08:00:00Z",
  previous_observed: null, previous_session_verified: null, reference_price: null, change: null, change_percent: null,
  change_unavailable_reason: "official_reference_not_available",
};
let resizeCallback: ResizeObserverCallback;

beforeEach(() => {
  vi.clearAllMocks();
  chartMock.addSeries.mockImplementation((series: { type: string }) => series.type === "candles" ? chartMock.candles : chartMock.volume);
  chartMock.create.mockReturnValue({
    addSeries: chartMock.addSeries, resize: chartMock.resize, remove: chartMock.remove,
    subscribeCrosshairMove: chartMock.subscribe, unsubscribeCrosshairMove: chartMock.unsubscribe,
    timeScale: () => ({ fitContent: chartMock.fitContent }),
    panes: () => [{ setStretchFactor: chartMock.stretch }, { setStretchFactor: chartMock.stretch }],
  });
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
    observe = chartMock.observe;
    disconnect = chartMock.disconnect;
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(430);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("StockChart lifecycle", () => {
  it("creates actual candle and volume series, resizes and releases chart resources", async () => {
    const view = render(<StockChart bars={[bar]} timeframe="1w" />);
    await waitFor(() => expect(chartMock.candles.setData).toHaveBeenCalled());
    expect(chartMock.volume.setData).toHaveBeenCalledWith([expect.objectContaining({ value: 123456, time: "2026-01-09" })]);
    expect(chartMock.create).toHaveBeenCalledWith(expect.any(HTMLElement), expect.objectContaining({
      layout: expect.objectContaining({ attributionLogo: true }),
    }));
    act(() => resizeCallback([{ contentRect: { width: 480, height: 350 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(chartMock.resize).toHaveBeenCalledWith(480, 350);
    view.unmount();
    expect(chartMock.disconnect).toHaveBeenCalledOnce();
    expect(chartMock.unsubscribe).toHaveBeenCalledOnce();
    expect(chartMock.remove).toHaveBeenCalledOnce();
  });
  it("uses original decimal strings for crosshair tooltip and labels period uncertainty", async () => {
    render(<StockChart bars={[bar]} timeframe="1w" />);
    await waitFor(() => expect(chartMock.subscribe).toHaveBeenCalled());
    act(() => chartMock.subscribe.mock.calls[0][0]({ time: { year: 2026, month: 1, day: 9 }, point: { x: 200, y: 100 } }));
    expect(screen.getByLabelText("十字線行情")).toHaveTextContent("高 105.5 · 低 98");
    expect(screen.getByLabelText("十字線行情")).toHaveTextContent("期間完整度未驗證");
    act(() => chartMock.subscribe.mock.calls[0][0]({ time: undefined }));
    expect(screen.queryByLabelText("十字線行情")).not.toBeInTheDocument();
  });
  it("cleans up the old chart when switching timeframe", async () => {
    const view = render(<StockChart bars={[bar]} timeframe="1d" />);
    await waitFor(() => expect(chartMock.create).toHaveBeenCalledOnce());
    view.rerender(<StockChart bars={[bar]} timeframe="1M" />);
    await waitFor(() => expect(chartMock.create).toHaveBeenCalledTimes(2));
    expect(chartMock.remove).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("月 K與成交量圖")).toBeInTheDocument();
  });
  it("does not create a chart for empty or invalid data", () => {
    const view = render(<StockChart bars={[]} timeframe="1d" />);
    expect(screen.getByRole("status")).toHaveTextContent("沒有已收錄行情");
    view.rerender(<StockChart bars={[{ ...bar, close: "Infinity" }]} timeframe="1d" />);
    expect(screen.getByRole("alert")).toHaveTextContent("無法安全繪製");
    expect(chartMock.create).not.toHaveBeenCalled();
  });
  it("ignores an import that completes after unmount", async () => {
    const view = render(<StockChart bars={[bar]} timeframe="1d" />);
    view.unmount();
    await act(async () => { await Promise.resolve(); });
    expect(chartMock.create).not.toHaveBeenCalled();
  });
  it("shows a recoverable error when the browser cannot create a chart", async () => {
    chartMock.create.mockImplementationOnce(() => { throw new Error("canvas unavailable"); });
    render(<StockChart bars={[bar]} timeframe="1d" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("圖表暫時無法開啟");
  });
  it("also releases resources if initialization fails after creating the chart", async () => {
    chartMock.addSeries.mockImplementationOnce(() => { throw new Error("series unavailable"); });
    const view = render(<StockChart bars={[bar]} timeframe="1d" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("圖表暫時無法開啟");
    expect(chartMock.remove).toHaveBeenCalledOnce();
    view.unmount();
    expect(chartMock.remove).toHaveBeenCalledOnce();
  });
});
