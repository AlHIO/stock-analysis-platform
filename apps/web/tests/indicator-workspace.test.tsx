import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IndicatorWorkspace } from "../src/components/indicator-workspace";
import { getIndicator } from "../src/lib/technical-api";
import type { IndicatorResponse } from "../src/lib/technical-types";
import { barsFixture, instrumentFixture } from "./fixtures/market-api";
import { indicatorFixture } from "./fixtures/technical";
vi.mock("../src/lib/technical-api", () => ({ getIndicator: vi.fn() }));
vi.mock("../src/components/stock-chart", () => ({
  StockChart: ({ indicators }: { indicators: IndicatorResponse[] }) => (
    <div
      data-testid="indicator-chart"
      data-indicators={indicators
        .map(
          (item) =>
            `${item.timeframe}:${item.indicator}:${Object.values(item.parameters).join("/")}`,
        )
        .join(",")}
    />
  ),
}));
const api = vi.mocked(getIndicator);
const props = {
  instrumentId: instrumentFixture.id,
  bars: barsFixture.bars,
  timeframe: "1d" as const,
  range: { from: "2026-01-05", to: "2026-01-05" },
};
beforeEach(() =>
  api.mockImplementation(async (_id, timeframe, name, parameters) =>
    indicatorFixture(name, { timeframe, parameters }),
  ),
);
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("indicator workspace", () => {
  it("reloads computed history when refreshed bars arrive with the same range", async () => {
    const view = render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /SMA/ }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    view.rerender(
      <IndicatorWorkspace
        {...props}
        bars={props.bars.map((bar) => ({ ...bar, close: "13.00000000" }))}
      />,
    );
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
  });
  it("does not compute or request disabled indicators", () => {
    render(<IndicatorWorkspace {...props} />);
    expect(api).not.toHaveBeenCalled();
    expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
      "data-indicators",
      "",
    );
  });
  it("toggles a backend SMA overlay on and off", async () => {
    render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /SMA/ }));
    await waitFor(() =>
      expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
        "data-indicators",
        "1d:sma:20",
      ),
    );
    expect(api).toHaveBeenCalledWith(
      props.instrumentId,
      "1d",
      "sma",
      { period: 20 },
      props.range,
      expect.any(AbortSignal),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /SMA/ }));
    expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
      "data-indicators",
      "",
    );
  });
  it("applies parameters on submit, not on draft keystrokes", async () => {
    render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /SMA/ }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("SMA 期數"), {
      target: { value: "3" },
    });
    expect(api).toHaveBeenCalledTimes(1);
    fireEvent.submit(screen.getByLabelText("SMA 期數").closest("form")!);
    await waitFor(() =>
      expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
        "data-indicators",
        "1d:sma:3",
      ),
    );
  });
  it("rejects invalid MACD periods without changing applied settings", async () => {
    render(<IndicatorWorkspace {...props} />);
    fireEvent.change(screen.getByLabelText("MACD 快線期數"), {
      target: { value: "30" },
    });
    fireEvent.submit(screen.getByLabelText("MACD 快線期數").closest("form")!);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "快線期數必須小於慢線期數",
    );
    expect(api).not.toHaveBeenCalled();
  });
  it("keeps the chart visible when indicators fail and supports retry", async () => {
    api.mockRejectedValueOnce(new Error("private upstream"));
    render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /RSI/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "RSI 暫時無法載入",
    );
    expect(screen.getByTestId("indicator-chart")).toBeInTheDocument();
    expect(screen.queryByText("private upstream")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重試指標" }));
    await waitFor(() =>
      expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
        "data-indicators",
        "1d:rsi:14",
      ),
    );
  });
  it("shows warmup without presenting null as zero", async () => {
    const response = indicatorFixture("rsi");
    response.points[0] = {
      ...response.points[0],
      values: { rsi: null },
      status: "warmup",
    };
    api.mockResolvedValue(response);
    render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /RSI/ }));
    expect(
      await screen.findByText(/需要 15 根連續有效 K 棒/),
    ).toBeInTheDocument();
    expect(screen.queryByText("RSI 0")).not.toBeInTheDocument();
  });
  it("retains toggles through timeframe loading and discards stale indicator responses", async () => {
    let resolve!: (value: IndicatorResponse) => void;
    api.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const view = render(<IndicatorWorkspace {...props} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /SMA/ }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    const signal = api.mock.calls[0][5];
    view.rerender(
      <IndicatorWorkspace
        {...props}
        bars={null}
        timeframe="1w"
        placeholder={<p>週 K 載入中</p>}
      />,
    );
    expect(screen.getByRole("checkbox", { name: /SMA/ })).toBeChecked();
    expect(signal?.aborted).toBe(true);
    view.rerender(<IndicatorWorkspace {...props} timeframe="1w" />);
    await waitFor(() =>
      expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
        "data-indicators",
        "1w:sma:20",
      ),
    );
    await act(async () => resolve(indicatorFixture()));
    expect(screen.getByTestId("indicator-chart")).toHaveAttribute(
      "data-indicators",
      "1w:sma:20",
    );
  });
  it("does not request or draw indicators without saved bars", async () => {
    render(<IndicatorWorkspace {...props} bars={[]} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /MACD/ }));
    expect(
      screen.getByText("目前沒有行情，無法計算指標。"),
    ).toBeInTheDocument();
    expect(api).not.toHaveBeenCalled();
    expect(screen.queryByTestId("indicator-chart")).not.toBeInTheDocument();
  });
});
