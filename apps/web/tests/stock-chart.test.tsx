import React from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Bar } from "../src/lib/market-types";
import { StockChart } from "../src/components/stock-chart";
import type {
  IndicatorName,
  IndicatorResponse,
} from "../src/lib/technical-types";
import { latestFixture } from "./fixtures/market-api";

const chartMock = vi.hoisted(() => {
  const series = () => ({ setData: vi.fn(), createPriceLine: vi.fn() });
  return {
    create: vi.fn(),
    candles: series(),
    volume: series(),
    addSeries: vi.fn(),
    resize: vi.fn(),
    remove: vi.fn(),
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
    fitContent: vi.fn(),
    stretch: vi.fn(),
    disconnect: vi.fn(),
    observe: vi.fn(),
    lines: [] as ReturnType<typeof series>[],
    histograms: [] as ReturnType<typeof series>[],
    series,
  };
});
vi.mock("lightweight-charts", () => ({
  createChart: chartMock.create,
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  CandlestickSeries: { type: "candles" },
  HistogramSeries: { type: "volume" },
  LineSeries: { type: "line" },
  LineStyle: { Dashed: 2 },
}));

const bar: Bar = {
  trade_date: "2026-01-09",
  period_start: "2026-01-05",
  period_end: "2026-01-11",
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
};
let resizeCallback: ResizeObserverCallback;
function indicator(
  name: IndicatorName,
  values: Record<string, string | null>,
): IndicatorResponse {
  return {
    ...latestFixture,
    from: bar.trade_date,
    to: bar.trade_date,
    count: 1,
    precision: {
      internal_digits: 50,
      output_decimal_places: 12,
      rounding: "ROUND_HALF_EVEN",
    },
    calculation: {
      seed_policy: "full_available_history",
      history_from: bar.trade_date,
      input_count: 1,
      prehistory_count: 0,
      missing_policy: "reset_on_explicit_missing",
      calendar_policy: "observed_sessions_with_known_gaps",
      history_completeness: "not_verified",
    },
    indicator: name,
    algorithm_version: "1.0.0",
    parameters:
      name === "macd"
        ? { fast_period: 2, slow_period: 3, signal_period: 2 }
        : { period: 2 },
    timeframe: "1d",
    price_basis: "raw",
    field: name === "volume_sma" ? "volume_shares" : "close",
    required_samples: 2,
    points: [
      {
        trade_date: bar.trade_date,
        values,
        status: Object.values(values).some((value) => value === null)
          ? "warmup"
          : "ready",
        is_provisional: false,
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  chartMock.lines.length = 0;
  chartMock.histograms.length = 0;
  chartMock.addSeries.mockImplementation(
    (definition: { type: string }, options: { title?: string } = {}) => {
      if (definition.type === "candles") return chartMock.candles;
      if (definition.type === "line") {
        const result = chartMock.series();
        chartMock.lines.push(result);
        return result;
      }
      if (options.title === "MACD HIST") {
        const result = chartMock.series();
        chartMock.histograms.push(result);
        return result;
      }
      return chartMock.volume;
    },
  );
  chartMock.create.mockReturnValue({
    addSeries: chartMock.addSeries,
    resize: chartMock.resize,
    remove: chartMock.remove,
    subscribeCrosshairMove: chartMock.subscribe,
    unsubscribeCrosshairMove: chartMock.unsubscribe,
    timeScale: () => ({ fitContent: chartMock.fitContent }),
    panes: () =>
      Array.from({ length: 4 }, () => ({
        setStretchFactor: chartMock.stretch,
      })),
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }
      observe = chartMock.observe;
      disconnect = chartMock.disconnect;
    },
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(430);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("StockChart lifecycle", () => {
  it("creates actual candle and volume series, resizes and releases chart resources", async () => {
    const view = render(<StockChart bars={[bar]} timeframe="1w" />);
    await waitFor(() => expect(chartMock.candles.setData).toHaveBeenCalled());
    expect(chartMock.volume.setData).toHaveBeenCalledWith([
      expect.objectContaining({ value: 123456, time: "2026-01-09" }),
    ]);
    expect(chartMock.create).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({
        layout: expect.objectContaining({ attributionLogo: true }),
      }),
    );
    act(() =>
      resizeCallback(
        [{ contentRect: { width: 480, height: 350 } } as ResizeObserverEntry],
        {} as ResizeObserver,
      ),
    );
    expect(chartMock.resize).toHaveBeenCalledWith(480, 350);
    view.unmount();
    expect(chartMock.disconnect).toHaveBeenCalledOnce();
    expect(chartMock.unsubscribe).toHaveBeenCalledOnce();
    expect(chartMock.remove).toHaveBeenCalledOnce();
  });
  it("uses original decimal strings for crosshair tooltip and labels period uncertainty", async () => {
    render(<StockChart bars={[bar]} timeframe="1w" />);
    await waitFor(() => expect(chartMock.subscribe).toHaveBeenCalled());
    act(() =>
      chartMock.subscribe.mock.calls[0][0]({
        time: { year: 2026, month: 1, day: 9 },
        point: { x: 200, y: 100 },
      }),
    );
    expect(screen.getByLabelText("十字線行情")).toHaveTextContent(
      "高 105.5 · 低 98",
    );
    expect(screen.getByLabelText("十字線行情")).toHaveTextContent(
      "期間完整度未驗證",
    );
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
    view.rerender(
      <StockChart bars={[{ ...bar, close: "Infinity" }]} timeframe="1d" />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("無法安全繪製");
    expect(chartMock.create).not.toHaveBeenCalled();
  });
  it("ignores an import that completes after unmount", async () => {
    const view = render(<StockChart bars={[bar]} timeframe="1d" />);
    view.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(chartMock.create).not.toHaveBeenCalled();
  });
  it("shows a recoverable error when the browser cannot create a chart", async () => {
    chartMock.create.mockImplementationOnce(() => {
      throw new Error("canvas unavailable");
    });
    render(<StockChart bars={[bar]} timeframe="1d" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "圖表暫時無法開啟",
    );
  });
  it("also releases resources if initialization fails after creating the chart", async () => {
    chartMock.addSeries.mockImplementationOnce(() => {
      throw new Error("series unavailable");
    });
    const view = render(<StockChart bars={[bar]} timeframe="1d" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "圖表暫時無法開啟",
    );
    expect(chartMock.remove).toHaveBeenCalledOnce();
    view.unmount();
    expect(chartMock.remove).toHaveBeenCalledOnce();
  });
  it("renders overlays, a volume average, RSI and MACD in their own panes", async () => {
    render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[
          indicator("sma", { sma: "103.123456789012" }),
          indicator("ema", { ema: "102" }),
          indicator("volume_sma", { volume_sma: "123.5" }),
          indicator("rsi", { rsi: "55" }),
          indicator("macd", { macd: "-1.5", signal: "-1", histogram: "-0.5" }),
        ]}
      />,
    );
    await waitFor(() => expect(chartMock.lines).toHaveLength(6));
    expect(chartMock.addSeries).toHaveBeenCalledWith(
      { type: "line" },
      expect.objectContaining({ title: "SMA(2)" }),
      0,
    );
    expect(chartMock.addSeries).toHaveBeenCalledWith(
      { type: "line" },
      expect.objectContaining({ title: "VOL MA(2)" }),
      1,
    );
    expect(chartMock.addSeries).toHaveBeenCalledWith(
      { type: "line" },
      expect.objectContaining({ title: "RSI(2)" }),
      2,
    );
    expect(chartMock.addSeries).toHaveBeenCalledWith(
      { type: "volume" },
      expect.objectContaining({ title: "MACD HIST" }),
      3,
    );
    expect(chartMock.lines[3].createPriceLine).toHaveBeenCalledTimes(2);
    const rsiOptions = chartMock.addSeries.mock.calls.find(
      ([, options]) => options?.title === "RSI(2)",
    )![1];
    expect(rsiOptions.autoscaleInfoProvider()).toEqual({
      priceRange: { minValue: 0, maxValue: 100 },
    });
    expect(chartMock.histograms[0].setData).toHaveBeenCalledWith([
      expect.objectContaining({ value: -0.5 }),
    ]);
    expect(screen.getByLabelText("已顯示技術指標")).toHaveTextContent(
      "103.123456789012",
    );
    expect(document.querySelector('[data-subpanes="2"]')).toBeInTheDocument();
  });
  it("shows a warmup state with no invented indicator value", async () => {
    render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[indicator("rsi", { rsi: null })]}
      />,
    );
    await waitFor(() => expect(chartMock.lines).toHaveLength(1));
    expect(chartMock.lines[0].setData).toHaveBeenCalledWith([]);
    expect(chartMock.lines[0].createPriceLine).not.toHaveBeenCalled();
    expect(screen.getByLabelText("已顯示技術指標")).toHaveTextContent(
      "暖機／資料不足",
    );
    expect(screen.getByLabelText("已顯示技術指標")).toHaveTextContent("—");
  });
  it("keeps an available MACD line while signal and histogram are warming up", async () => {
    render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[
          indicator("macd", {
            macd: "1.250000000000",
            signal: null,
            histogram: null,
          }),
        ]}
      />,
    );
    await waitFor(() => expect(chartMock.lines).toHaveLength(2));
    expect(chartMock.lines[0].setData).toHaveBeenCalledWith([
      { time: bar.trade_date, value: 1.25 },
    ]);
    expect(chartMock.lines[1].setData).toHaveBeenCalledWith([]);
    expect(chartMock.histograms[0].setData).toHaveBeenCalledWith([
      { time: bar.trade_date },
    ]);
    expect(screen.getByLabelText("已顯示技術指標")).toHaveTextContent(
      "MACD 1.25",
    );
    expect(screen.getByLabelText("已顯示技術指標")).toHaveTextContent(
      "暖機／資料不足",
    );
  });
  it("renders distinct line series across an explicit missing-input gap", async () => {
    const middle = { ...bar, trade_date: "2026-01-12" };
    const last = { ...bar, trade_date: "2026-01-13" };
    const response = indicator("ema", { ema: "100" });
    response.points = [
      response.points[0],
      {
        ...response.points[0],
        trade_date: middle.trade_date,
        values: { ema: null },
        status: "missing_input",
      },
      {
        ...response.points[0],
        trade_date: last.trade_date,
        values: { ema: "102" },
      },
    ];
    render(
      <StockChart
        bars={[bar, middle, last]}
        timeframe="1d"
        indicators={[response]}
      />,
    );
    await waitFor(() => expect(chartMock.lines).toHaveLength(2));
    expect(chartMock.lines[0].setData).toHaveBeenCalledWith([
      { time: bar.trade_date, value: 100 },
    ]);
    expect(chartMock.lines[1].setData).toHaveBeenCalledWith([
      { time: last.trade_date, value: 102 },
    ]);
  });
  it("retains candle rendering when an indicator cannot be safely drawn", async () => {
    render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[indicator("sma", { sma: "Infinity" })]}
      />,
    );
    await waitFor(() => expect(chartMock.candles.setData).toHaveBeenCalled());
    expect(screen.getByRole("alert")).toHaveTextContent(
      "部分指標數值無法安全繪圖",
    );
    expect(chartMock.lines).toHaveLength(0);
  });
  it("preserves exact indicator text in the crosshair tooltip", async () => {
    render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[indicator("ema", { ema: "102.123456789012" })]}
      />,
    );
    await waitFor(() => expect(chartMock.subscribe).toHaveBeenCalled());
    act(() =>
      chartMock.subscribe.mock.calls[0][0]({
        time: bar.trade_date,
        point: { x: 200, y: 100 },
      }),
    );
    expect(screen.getByLabelText("十字線行情")).toHaveTextContent(
      "EMA 102.123456789012",
    );
  });
  it("removes indicator series with the chart when visibility changes", async () => {
    const view = render(
      <StockChart
        bars={[bar]}
        timeframe="1d"
        indicators={[indicator("ema", { ema: "102" })]}
      />,
    );
    await waitFor(() => expect(chartMock.lines).toHaveLength(1));
    view.rerender(<StockChart bars={[bar]} timeframe="1d" indicators={[]} />);
    await waitFor(() => expect(chartMock.create).toHaveBeenCalledTimes(2));
    expect(chartMock.remove).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("已顯示技術指標")).not.toBeInTheDocument();
  });
});
