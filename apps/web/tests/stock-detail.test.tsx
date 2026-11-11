import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StockDetail } from "../src/components/stock-detail";
import { ApiError, getBars, getLatest } from "../src/lib/market-api";
import type {
  Bar,
  BarsResponse,
  LatestResponse,
  Timeframe,
} from "../src/lib/market-types";
import {
  barsFixture,
  instrumentFixture,
  latestFixture,
} from "./fixtures/market-api";

vi.mock("../src/lib/market-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/market-api")>()),
  getBars: vi.fn(),
  getLatest: vi.fn(),
}));
vi.mock("../src/components/stock-search", () => ({
  StockSearch: () => <div aria-label="測試搜尋" />,
}));
// Keep these existing price-page unit tests scoped to price behavior. The real
// child integration is exercised by ChipDashboard tests and browser E2E.
vi.mock("../src/components/chip-dashboard", () => ({
  ChipDashboard: () => <div aria-label="籌碼分析測試區" />,
}));
vi.mock("../src/components/stock-chart", () => ({
  StockChart: ({ bars, timeframe }: { bars: Bar[]; timeframe: Timeframe }) => (
    <div data-testid="stock-chart" data-timeframe={timeframe}>
      {bars.length} 根測試 K 棒
    </div>
  ),
}));
vi.mock("next/link", () => ({
  default: ({
    children,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props}>{children}</a>
  ),
}));
const latest = vi.mocked(getLatest);
const bars = vi.mocked(getBars);
const id = instrumentFixture.id;
const reply = (timeframe: Timeframe): BarsResponse => ({
  ...barsFixture,
  timeframe,
});
const longHistory: LatestResponse = {
  ...latestFixture,
  history: {
    available_from: "2016-11-07",
    available_to: "2026-11-05",
    completeness: "not_verified",
  },
};

// Synthetic sparse history, used only to verify visible coverage boundaries.
const observedBar = (date: string, changes: Partial<Bar> = {}): Bar => ({
  ...barsFixture.bars[0],
  trade_date: date,
  first_trade_date: date,
  last_trade_date: date,
  period_start: date,
  period_end: date,
  ...changes,
});
const sparseHistory = [
  observedBar("2016-11-07"),
  observedBar("2026-08-28", {
    first_trade_date: "2026-08-25",
    period_start: "2026-08-24",
    period_end: "2026-08-30",
    observed_session_count: 4,
  }),
  observedBar("2026-11-05"),
];

function mockSparseHistory() {
  latest.mockResolvedValue({
    ...longHistory,
    freshness: {
      ...longHistory.freshness,
      market_local_date: "2026-11-05",
      latest_trade_date: "2026-11-05",
    },
  });
  bars.mockImplementation(async (_id, timeframe, _signal, range) => {
    const from = range?.from ?? "2025-11-05";
    const to = range?.to ?? "2026-11-05";
    const included = sparseHistory.filter(
      (bar) => bar.last_trade_date >= from && bar.first_trade_date <= to,
    );
    return {
      ...barsFixture,
      timeframe,
      from,
      to,
      bars: included,
      count: included.length,
    };
  });
}

beforeEach(() => {
  latest.mockResolvedValue(latestFixture);
  bars.mockImplementation(async (_id, timeframe) => reply(timeframe));
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("StockDetail", () => {
  it("shows loading without a price or chart until the stored snapshot arrives", async () => {
    let resolve!: (value: LatestResponse) => void;
    latest.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    render(<StockDetail id={id} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在讀取已收錄行情");
    expect(
      screen.queryByRole("region", { name: "最新日行情" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("stock-chart")).not.toBeInTheDocument();
    expect(bars).not.toHaveBeenCalled();
    await act(async () => {
      resolve(latestFixture);
    });
    expect(
      await screen.findByRole("heading", { name: /測試公司/ }),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1d",
    );
  });

  it("shows a 404 as a missing stock and offers a return to search", async () => {
    latest.mockRejectedValueOnce(
      new ApiError(404, "stock_not_found", "missing"),
    );
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "找不到這檔股票",
    );
    expect(screen.getByRole("link", { name: "返回搜尋" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(
      screen.queryByRole("button", { name: "重新載入" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("stock-chart")).not.toBeInTheDocument();
    expect(bars).not.toHaveBeenCalled();
  });

  it("shows API unavailable for a 503 and retries the snapshot successfully", async () => {
    latest.mockRejectedValueOnce(
      new ApiError(503, "api_unavailable", "private upstream details"),
    );
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "行情 API 暫時無法連線",
    );
    expect(
      screen.queryByText("private upstream details"),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新載入" }));
    expect(await screen.findByTestId("stock-chart")).toBeInTheDocument();
    expect(latest).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an honest no-data state without constructing a chart or a price", async () => {
    latest.mockResolvedValue({
      ...latestFixture,
      bar: null,
      history: {
        available_from: null,
        available_to: null,
        completeness: "not_verified",
      },
      freshness: {
        ...latestFixture.freshness,
        assessment: "no_data",
        latest_trade_date: null,
        last_ingested_at: null,
        age_calendar_days: null,
      },
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
    expect(
      await screen.findByText("目前顯示較早日期的已收錄資料"),
    ).toBeInTheDocument();
    expect(screen.getByText(/距市場本地日期 1 個日曆日/)).toHaveTextContent(
      "此差距不代表遺漏交易日",
    );
    expect(screen.getByText(/距市場本地日期 1 個日曆日/)).toHaveTextContent(
      "尚未驗證交易日曆與來源是否已更新",
    );
    expect(
      await screen.findByText(/目前全部歷史完整性未驗證/),
    ).toBeInTheDocument();
  });

  it("queries the backend for day, week and month rather than aggregating in the UI", async () => {
    render(<StockDetail id={id} />);
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1d",
    );
    expect(bars).toHaveBeenLastCalledWith(
      id,
      "1d",
      expect.any(AbortSignal),
      expect.any(Object),
    );
    fireEvent.click(screen.getByRole("button", { name: "週 K" }));
    await act(async () => {});
    expect(screen.getByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1w",
    );
    expect(bars).toHaveBeenLastCalledWith(
      id,
      "1w",
      expect.any(AbortSignal),
      expect.any(Object),
    );
    expect(screen.getByRole("button", { name: "週 K" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "月 K" }));
    await act(async () => {});
    expect(screen.getByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1M",
    );
    expect(bars).toHaveBeenLastCalledWith(
      id,
      "1M",
      expect.any(AbortSignal),
      expect.any(Object),
    );
    expect(screen.getByRole("button", { name: "月 K" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(bars.mock.calls.map((call) => call[1])).toEqual(["1d", "1w", "1M"]);
  });

  it("ignores a slow week response after the user switches to month", async () => {
    let resolveWeek!: (value: BarsResponse) => void;
    bars.mockImplementation(async (_id, timeframe) =>
      timeframe === "1w"
        ? new Promise((resolve) => {
            resolveWeek = resolve;
          })
        : reply(timeframe),
    );
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    fireEvent.click(screen.getByRole("button", { name: "週 K" }));
    expect(screen.getByText("正在載入週 K…")).toBeInTheDocument();
    const weekSignal = bars.mock.calls.find((call) => call[1] === "1w")?.[2];
    fireEvent.click(screen.getByRole("button", { name: "月 K" }));
    await act(async () => {});
    expect(weekSignal?.aborted).toBe(true);
    expect(screen.getByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1M",
    );
    await act(async () => {
      resolveWeek(reply("1w"));
    });
    expect(screen.getByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1M",
    );
    expect(screen.queryByText("正在載入月 K…")).not.toBeInTheDocument();
  });

  it("retries chart failures without discarding the latest stored snapshot", async () => {
    bars.mockRejectedValueOnce(
      new ApiError(503, "api_unavailable", "hidden upstream failure"),
    );
    render(<StockDetail id={id} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "行情 API 暫時無法連線",
    );
    expect(
      screen.getByRole("heading", { name: /測試公司/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("12.5")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新載入" }));
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1d",
    );
    expect(latest).toHaveBeenCalledTimes(1);
    expect(bars).toHaveBeenCalledTimes(2);
  });

  it("requests the saved ten-year range with weekly candles before reaching the daily limit", async () => {
    latest.mockResolvedValue(longHistory);
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    bars.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "最近 10 年" }));
    await waitFor(() =>
      expect(bars).toHaveBeenLastCalledWith(id, "1w", expect.any(AbortSignal), {
        from: "2016-11-07",
        to: "2026-11-05",
      }),
    );
    expect(screen.getByRole("button", { name: "週 K" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1w",
    );
    expect(screen.getByRole("button", { name: "日 K" })).toBeDisabled();
    bars.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "日 K" }));
    expect(bars).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);
  });

  it("only queries a custom older daily interval after the dates are applied", async () => {
    latest.mockResolvedValue(longHistory);
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    bars.mockClear();
    fireEvent.change(screen.getByLabelText("開始日期"), {
      target: { value: "2016-11-07" },
    });
    fireEvent.change(screen.getByLabelText("結束日期"), {
      target: { value: "2016-11-08" },
    });
    expect(bars).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "套用日期" }));
    await waitFor(() =>
      expect(bars).toHaveBeenLastCalledWith(id, "1d", expect.any(AbortSignal), {
        from: "2016-11-07",
        to: "2016-11-08",
      }),
    );
    expect(await screen.findByTestId("stock-chart")).toHaveAttribute(
      "data-timeframe",
      "1d",
    );
    expect(latest).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["2026-11-06", "2026-11-05"],
    ["2010-11-07", "2026-11-05"],
    ["", "2026-11-05"],
  ])(
    "rejects invalid date range %s to %s without another API request",
    async (start, end) => {
      latest.mockResolvedValue(longHistory);
      render(<StockDetail id={id} />);
      await screen.findByTestId("stock-chart");
      bars.mockClear();
      fireEvent.change(screen.getByLabelText("開始日期"), {
        target: { value: start },
      });
      fireEvent.change(screen.getByLabelText("結束日期"), {
        target: { value: end },
      });
      fireEvent.submit(screen.getByRole("form", { name: "自訂歷史日期" }));
      expect(await screen.findByRole("alert")).toBeVisible();
      expect(bars).not.toHaveBeenCalled();
      expect(latest).toHaveBeenCalledTimes(1);
    },
  );

  it("reloads the stored history extent and latest snapshot without calling an exchange", async () => {
    latest.mockResolvedValueOnce(longHistory).mockResolvedValueOnce({
      ...longHistory,
      history: { ...longHistory.history, available_to: "2026-11-06" },
    });
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    bars.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "重新讀取資料" }));
    await waitFor(() => expect(latest).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(bars).toHaveBeenLastCalledWith(id, "1d", expect.any(AbortSignal), {
        from: "2025-11-06",
        to: "2026-11-06",
      }),
    );
    expect(screen.getByLabelText("結束日期")).toHaveValue("2026-11-06");
  });

  it("preserves a custom older interval when newly imported data is refreshed", async () => {
    latest.mockResolvedValueOnce(longHistory).mockResolvedValueOnce({
      ...longHistory,
      history: {
        ...longHistory.history,
        available_from: "2015-11-07",
        available_to: "2026-11-06",
      },
    });
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    fireEvent.change(screen.getByLabelText("開始日期"), {
      target: { value: "2016-11-07" },
    });
    fireEvent.change(screen.getByLabelText("結束日期"), {
      target: { value: "2016-11-08" },
    });
    fireEvent.click(screen.getByRole("button", { name: "套用日期" }));
    await waitFor(() =>
      expect(bars).toHaveBeenLastCalledWith(id, "1d", expect.any(AbortSignal), {
        from: "2016-11-07",
        to: "2016-11-08",
      }),
    );
    bars.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "重新讀取資料" }));
    await waitFor(() => expect(latest).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(bars).toHaveBeenLastCalledWith(id, "1d", expect.any(AbortSignal), {
        from: "2016-11-07",
        to: "2016-11-08",
      }),
    );
    expect(screen.getByLabelText("開始日期")).toHaveValue("2016-11-07");
    expect(screen.getByLabelText("結束日期")).toHaveValue("2016-11-08");
  });

  it("warns above a sparse ten-year chart using the observed aggregate interval", async () => {
    mockSparseHistory();
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    expect(
      screen.queryByRole("status", { name: "歷史資料日期間隔" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "最近 10 年" }));
    const notice = await screen.findByRole("status", {
      name: "歷史資料日期間隔",
    });
    expect(
      within(notice).getByText("這段歷史資料尚未連續收錄"),
    ).toBeVisible();
    expect(notice).toHaveTextContent("2016/11/07");
    expect(notice).toHaveTextContent("2026/08/25");
    expect(notice).not.toHaveTextContent("2026/08/24");
    expect(screen.getByTestId("stock-chart")).toHaveTextContent(
      "3 根測試 K 棒",
    );
    expect(
      notice.compareDocumentPosition(screen.getByTestId("stock-chart")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it.each([
    ["週 K", "1w", "2026-08-24"],
    ["月 K", "1M", "2026-08-01"],
  ] as const)(
    "selects only the recent observed segment and preserves %s",
    async (label, timeframe, alignedFrom) => {
      mockSparseHistory();
      const before = structuredClone(sparseHistory);
      render(<StockDetail id={id} />);
      await screen.findByTestId("stock-chart");
      fireEvent.click(screen.getByRole("button", { name: "最近 10 年" }));
      await screen.findByRole("status", { name: "歷史資料日期間隔" });
      if (timeframe === "1M") {
        fireEvent.click(screen.getByRole("button", { name: label }));
        await waitFor(() =>
          expect(screen.getByTestId("stock-chart")).toHaveAttribute(
            "data-timeframe",
            timeframe,
          ),
        );
      }
      bars.mockClear();
      fireEvent.click(
        screen.getByRole("button", { name: "只看最近已收錄區段" }),
      );
      await waitFor(() =>
        expect(bars).toHaveBeenLastCalledWith(
          id,
          timeframe,
          expect.any(AbortSignal),
          {
            from: alignedFrom,
            to: "2026-11-05",
          },
        ),
      );
      await waitFor(() =>
        expect(
          screen.queryByRole("status", { name: "歷史資料日期間隔" }),
        ).not.toBeInTheDocument(),
      );
      expect(screen.getByLabelText("開始日期")).toHaveValue("2026-08-25");
      expect(screen.getByRole("button", { name: label })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByTestId("stock-chart")).toHaveTextContent(
        "2 根測試 K 棒",
      );
      expect(sparseHistory).toEqual(before);
    },
  );

  it("does not display a long-gap notice before the selected range has loaded", async () => {
    mockSparseHistory();
    let resolveWeek!: (value: BarsResponse) => void;
    const current = bars.getMockImplementation()!;
    bars.mockImplementation(async (...args) =>
      args[1] === "1w"
        ? new Promise((resolve) => {
            resolveWeek = resolve;
          })
        : current(...args),
    );
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    fireEvent.click(screen.getByRole("button", { name: "最近 10 年" }));
    expect(screen.getByText("正在載入週 K…")).toBeVisible();
    expect(
      screen.queryByRole("status", { name: "歷史資料日期間隔" }),
    ).not.toBeInTheDocument();
    await act(async () =>
      resolveWeek({
        ...barsFixture,
        timeframe: "1w",
        from: "2016-11-07",
        to: "2026-11-05",
        bars: sparseHistory,
        count: sparseHistory.length,
      }),
    );
    expect(
      await screen.findByRole("status", { name: "歷史資料日期間隔" }),
    ).toBeVisible();
  });

  it("does not infer missing trading sessions from a short observed date gap", async () => {
    const ordinary = [observedBar("2026-01-09"), observedBar("2026-01-12")];
    latest.mockResolvedValue({
      ...latestFixture,
      history: {
        available_from: "2026-01-09",
        available_to: "2026-01-12",
        completeness: "not_verified",
      },
    });
    bars.mockResolvedValue({
      ...barsFixture,
      bars: ordinary,
      count: ordinary.length,
    });
    render(<StockDetail id={id} />);
    await screen.findByTestId("stock-chart");
    expect(
      screen.queryByRole("status", { name: "歷史資料日期間隔" }),
    ).not.toBeInTheDocument();
  });

  it("does not display the sparse-history notice for an empty stored range", async () => {
    bars.mockResolvedValue({ ...barsFixture, bars: [], count: 0 });
    render(<StockDetail id={id} />);
    expect(await screen.findByText("此範圍沒有行情資料")).toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: "歷史資料日期間隔" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "只看最近已收錄區段" }),
    ).not.toBeInTheDocument();
  });
});
