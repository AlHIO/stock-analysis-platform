import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StockDetail } from "../src/components/stock-detail";
import { ApiError, getBars, getLatest } from "../src/lib/market-api";
import type { Bar, BarsResponse, LatestResponse, Timeframe } from "../src/lib/market-types";
import { barsFixture, instrumentFixture, latestFixture } from "./fixtures/market-api";

vi.mock("../src/lib/market-api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/market-api")>(), getBars: vi.fn(), getLatest: vi.fn(),
}));
vi.mock("../src/components/stock-search", () => ({ StockSearch: () => <div aria-label="測試搜尋" /> }));
vi.mock("../src/components/stock-chart", () => ({
  StockChart: ({ bars, timeframe }: { bars: Bar[]; timeframe: Timeframe }) => <div data-testid="stock-chart" data-timeframe={timeframe}>{bars.length} 根測試 K 棒</div>,
}));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a {...props}>{children}</a> }));
const latest = vi.mocked(getLatest);
const bars = vi.mocked(getBars);
const id = instrumentFixture.id;
const reply = (timeframe: Timeframe): BarsResponse => ({ ...barsFixture, timeframe });

beforeEach(() => {
  latest.mockResolvedValue(latestFixture);
  bars.mockImplementation(async (_id, timeframe) => reply(timeframe));
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("StockDetail", () => {
  it("shows loading without a price or chart until the stored snapshot arrives", async () => {
    let resolve!: (value: LatestResponse) => void;
    latest.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    render(<StockDetail id={id} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在讀取已收錄行情");
    expect(screen.queryByRole("region", { name: "最新日行情" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("stock-chart")).not.toBeInTheDocument();
    expect(bars).not.toHaveBeenCalled();
    await act(async () => { resolve(latestFixture); });
    expect(await screen.findByRole("heading", { name: /測試公司/ })).toBeInTheDocument();
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1d");
  });

  it("shows a 404 as a missing stock and offers a return to search", async () => {
    latest.mockRejectedValueOnce(new ApiError(404, "stock_not_found", "missing"));
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("找不到這檔股票");
    expect(screen.getByRole("link", { name: "返回搜尋" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: "重新載入" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("stock-chart")).not.toBeInTheDocument();
    expect(bars).not.toHaveBeenCalled();
  });

  it("shows API unavailable for a 503 and retries the snapshot successfully", async () => {
    latest.mockRejectedValueOnce(new ApiError(503, "api_unavailable", "private upstream details"));
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("行情 API 暫時無法連線");
    expect(screen.queryByText("private upstream details")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新載入" }));
    expect(await screen.findByTestId("stock-chart")).toBeInTheDocument();
    expect(latest).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an honest no-data state without constructing a chart or a price", async () => {
    latest.mockResolvedValue({
      ...latestFixture, bar: null,
      history: { available_from: null, available_to: null, completeness: "not_verified" },
      freshness: { ...latestFixture.freshness, assessment: "no_data", latest_trade_date: null, last_ingested_at: null, age_calendar_days: null },
    });
    bars.mockResolvedValue({ ...barsFixture, bars: [], count: 0 });
    render(<StockDetail id={id} />);
    expect(await screen.findByText("此範圍沒有行情資料")).toBeInTheDocument();
    expect(screen.getByText(/這檔股票尚無已收錄日行情/)).toBeInTheDocument();
    expect(screen.queryByTestId("stock-chart")).not.toBeInTheDocument();
    const quote = screen.getByRole("region", { name: "最新日行情" });
    expect(within(quote).getByText("所屬交易日 —")).toBeInTheDocument();
    expect(within(quote).queryByText("0")).not.toBeInTheDocument();
  });

  it("keeps null changes unavailable and explains the unverified reference price", async () => {
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    const label = screen.getByText("漲跌 / 漲跌幅");
    const metric = label.closest(".metric");
    expect(metric).toHaveTextContent("— / —");
    expect(metric).toHaveTextContent("缺少可核實官方參考價時不計算");
    expect(metric).not.toHaveTextContent("0%");
    expect(screen.getByText("12.5")).toBeInTheDocument();
    expect(screen.getByText("12,345")).toBeInTheDocument();
    expect(screen.getByText("日行情快照 · 非即時")).toBeInTheDocument();
  });

  it("labels older data while explaining calendar-day age does not prove missing sessions", async () => {
    render(<StockDetail id={id} />);
    expect(await screen.findByText("目前顯示較早日期的已收錄資料")).toBeInTheDocument();
    expect(screen.getByText(/距市場本地日期 1 個日曆日/)).toHaveTextContent("此差距不代表遺漏交易日");
    expect(screen.getByText(/距市場本地日期 1 個日曆日/)).toHaveTextContent("尚未驗證交易日曆與來源是否已更新");
    expect(await screen.findByText(/目前全部歷史完整性未驗證/)).toBeInTheDocument();
  });

  it("queries the backend for day, week and month rather than aggregating in the UI", async () => {
    render(<StockDetail id={id} />);
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1d");
    expect(bars).toHaveBeenLastCalledWith(id, "1d", expect.any(AbortSignal), expect.any(Object));
    fireEvent.click(screen.getByRole("button", { name: "週 K" }));
    await act(async () => {});
    expect(screen.getByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1w");
    expect(bars).toHaveBeenLastCalledWith(id, "1w", expect.any(AbortSignal), expect.any(Object));
    expect(screen.getByRole("button", { name: "週 K" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "月 K" }));
    await act(async () => {});
    expect(screen.getByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1M");
    expect(bars).toHaveBeenLastCalledWith(id, "1M", expect.any(AbortSignal), expect.any(Object));
    expect(screen.getByRole("button", { name: "月 K" })).toHaveAttribute("aria-pressed", "true");
    expect(bars.mock.calls.map((call) => call[1])).toEqual(["1d", "1w", "1M"]);
  });

  it("ignores a slow week response after the user switches to month", async () => {
    let resolveWeek!: (value: BarsResponse) => void;
    bars.mockImplementation(async (_id, timeframe) => timeframe === "1w"
      ? new Promise((resolve) => { resolveWeek = resolve; }) : reply(timeframe));
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    fireEvent.click(screen.getByRole("button", { name: "週 K" }));
    expect(screen.getByText("正在載入週 K…")).toBeInTheDocument();
    const weekSignal = bars.mock.calls.find((call) => call[1] === "1w")?.[2];
    fireEvent.click(screen.getByRole("button", { name: "月 K" }));
    await act(async () => {});
    expect(weekSignal?.aborted).toBe(true);
    expect(screen.getByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1M");
    await act(async () => { resolveWeek(reply("1w")); });
    expect(screen.getByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1M");
    expect(screen.queryByText("正在載入月 K…")).not.toBeInTheDocument();
  });

  it("retries chart failures without discarding the latest stored snapshot", async () => {
    bars.mockRejectedValueOnce(new ApiError(503, "api_unavailable", "hidden upstream failure"));
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("行情 API 暫時無法連線");
    expect(screen.getByRole("heading", { name: /測試公司/ })).toBeInTheDocument();
    expect(screen.getByText("12.5")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新載入" }));
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute("data-timeframe", "1d");
    expect(latest).toHaveBeenCalledTimes(1);
    expect(bars).toHaveBeenCalledTimes(2);
  });
});
