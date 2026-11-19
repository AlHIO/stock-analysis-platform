/** SYNTHETIC broker UI observations only; never official data or production seeds. */
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrokerFlowPanel } from "../src/components/broker-flow-panel";
import * as api from "../src/lib/chip-api";
import { ApiError } from "../src/lib/market-api";
import type { BrokerAlgorithm, BrokerMetricResult, BrokerMetricsResponse, BrokerTopOptions } from "../src/lib/broker-types";
import type { ChipRange } from "../src/lib/chip-types";
import type { ChipSeries } from "../src/lib/chip-chart";
import { brokerHistoryFixture, brokerLatestFixture, brokerMetricFixture, brokerIds, brokerRangeFixture } from "./fixtures/broker";

vi.mock("../src/lib/chip-api", async original => ({ ...await original<typeof api>(),
  getBrokerHistory: vi.fn(), getBrokerLatest: vi.fn(), getBrokerTopBranches: vi.fn(), getBrokerMetrics: vi.fn(),
}));
vi.mock("../src/components/chip-chart", () => ({ ChipChart: ({ title, series, syncGroup }: { title: string; series: ChipSeries[]; syncGroup?: string }) =>
  <div data-testid={title} data-sync-group={syncGroup}>{JSON.stringify(series)}</div> }));

const id = brokerHistoryFixture().instrument.id;
const secondaryBranch = "00000000-0000-0000-0000-000000000002";
const secondaryIdentity = "00000000-0000-0000-0000-000000010002";
const secondaryScope = "00000000-0000-0000-0000-000000000306";

function top(range: ChipRange, options: BrokerTopOptions = {}): BrokerMetricsResponse {
  const sell = options.side === "net_sellers";
  const row = sell ? { branch_id: secondaryBranch, buy_quantity: "0", sell_quantity: "300", net_quantity: "-300", rank: 4, identity_ids: [secondaryIdentity] }
    : { branch_id: brokerIds.branch, buy_quantity: "1000", sell_quantity: "100", net_quantity: "900", rank: 2, identity_ids: [brokerIds.identity] };
  const data = brokerMetricFixture({ value: [row], parameters: { top_n: 15, side: options.side ?? "net_buyers" },
    details: options.sessions ? { session_window: { requested_sessions: options.sessions, verified: true,
      query_from: range.from, query_to: range.to, expected_dates: [range.from, range.to] } } : {} });
  data.result.request.start = range.from; data.result.request.end = range.to;
  data.result.input_refs[0].metadata.identities = [
    { ...brokerHistoryFixture().items[0].trades[0].identity },
    { ...brokerHistoryFixture().items[0].trades[0].identity, branch_id: secondaryBranch, identity_id: secondaryIdentity, branch_name: "SYNTHETIC 測試分點 B" },
  ];
  return data;
}

function metric(algorithm: BrokerAlgorithm, range: ChipRange, parameters: Record<string, unknown> = {}): BrokerMetricsResponse {
  const overrides: Partial<BrokerMetricResult> = { metric: algorithm,
    parameters: parameters as BrokerMetricResult["parameters"], value: "600", details: {} };
  if (algorithm === "proxy.broker_top_n_net_imbalance") overrides.details = {
    selected_buy_quantity: "1000", selected_sell_quantity: "400", selection_basis: "union_of_top_net_buyers_and_sellers",
  };
  else if (algorithm === "proxy.broker_daily_top_n_imbalance_sum") overrides.details = { daily: [{ trade_date: range.to, proxy_net: "600" }] };
  else if (algorithm === "broker.active_buy_sell_count_difference") {
    overrides.value = [{ trade_date: range.to, value: -1 }]; overrides.unit = "branches";
  } else if (algorithm === "broker.net_buy_sell_count_difference") {
    overrides.value = [{ trade_date: range.to, value: 0 }]; overrides.unit = "branches";
  } else overrides.value = "123456789012345678";
  const data = brokerMetricFixture(overrides);
  data.result.request.start = range.from; data.result.request.end = range.to;
  return data;
}

function boot(range = brokerRangeFixture) {
  return render(<BrokerFlowPanel id={id} range={range} active currency="TWD" syncGroup={`stock-${id}`}/>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getBrokerHistory).mockImplementation(async (_id, range, options) => ({ ...brokerHistoryFixture(), ...range, limit: options?.limit ?? 50 }));
  vi.mocked(api.getBrokerLatest).mockResolvedValue(brokerLatestFixture());
  vi.mocked(api.getBrokerTopBranches).mockImplementation(async (_id, range, options) => top(range, options));
  vi.mocked(api.getBrokerMetrics).mockImplementation(async (_id, algorithm, range, parameters) => metric(algorithm, range, parameters));
});
afterEach(cleanup);

describe("SYNTHETIC BrokerFlowPanel", () => {
  it("shows ranked source values and explicit Proxy semantics without investor or lot inference", async () => {
    boot();
    await screen.findByText("偏買超（Proxy）");
    const buyers = screen.getByRole("table", { name: /TOP 15 買超分點/ });
    const sellers = screen.getByRole("table", { name: /TOP 15 賣超分點/ });
    expect(within(buyers).getByText("900")).toBeVisible();
    expect(within(buyers).getByText("2")).toBeVisible();
    expect(within(sellers).getByText("-300")).toBeVisible();
    expect(within(sellers).getByText("4")).toBeVisible();
    expect(within(buyers).getByRole("button", { name: "SYNTHETIC 測試分點" })).toBeVisible();
    expect(screen.getByText("選中分點買進").closest("div")).toHaveTextContent("1,000");
    expect(screen.getByText("選中分點賣出").closest("div")).toHaveTextContent("400");
    expect(screen.getByText(/不假設一張 1,000 股/)).toBeVisible();
    expect(screen.getByText(/不是官方主力或真實中實戶/)).toBeVisible();
    expect(screen.getByText(/分點是交易中介/)).toBeVisible();
  });

  it("keeps active and net branch-count definitions distinct and plots backend signed observations", async () => {
    boot();
    const chart = await screen.findByTestId("分點買賣家數差");
    await waitFor(() => expect(chart).toHaveTextContent('"value":"-1"'));
    expect(chart).toHaveTextContent('"value":"0"');
    expect(screen.getByText("最新觀察日：有買進減有賣出家數").closest("div")).toHaveTextContent("-1");
    expect(screen.getByText("最新觀察日：淨買超減淨賣超家數").closest("div")).toHaveTextContent("0");
    expect(screen.getByTestId("每日重選 TOP 15 分點 Proxy")).toHaveAttribute("data-sync-group", `stock-${id}`);
    expect(screen.getByTestId("每日重選 TOP 15 分點 Proxy")).toHaveTextContent('"value":"600"');
  });

  it("sends session presets to the server and immediately hides old summary when the new window is unverified", async () => {
    boot(); await screen.findByText("偏買超（Proxy）");
    const priorMetrics = vi.mocked(api.getBrokerMetrics).mock.calls.length;
    vi.mocked(api.getBrokerTopBranches).mockImplementation(async (_id, range, options) => {
      const data = top(range, options); data.result.status = "not_verified"; data.result.value = null;
      data.result.details.session_window = { requested_sessions: options?.sessions ?? 5, verified: false, query_from: range.from, query_to: range.to, expected_dates: [] };
      return data;
    });
    fireEvent.click(screen.getByRole("button", { name: "5 個交易日" }));
    expect(screen.queryByText("偏買超（Proxy）")).not.toBeInTheDocument();
    await waitFor(() => expect(api.getBrokerTopBranches).toHaveBeenLastCalledWith(id, expect.anything(), expect.objectContaining({ sessions: 5 }), expect.any(AbortSignal)));
    await screen.findAllByText(/未取得核實完整區間/);
    expect(api.getBrokerMetrics).toHaveBeenCalledTimes(priorMetrics);
    expect(screen.queryByTestId("每日重選 TOP 15 分點 Proxy")).not.toBeInTheDocument();
  });

  it("copies a verified server window instead of calculating N trading dates in the browser", async () => {
    boot(); await screen.findByText("偏買超（Proxy）");
    vi.mocked(api.getBrokerTopBranches).mockImplementation(async (_id, range, options) => {
      const data = top(range, options);
      if (options?.sessions === 5) {
        data.result.request.start = "2025-12-24";
        data.result.request.end = "2026-01-02";
        data.result.details.session_window = { requested_sessions: 5, verified: true, query_from: range.from, query_to: range.to,
          expected_dates: ["2025-12-24", "2025-12-26", "2025-12-29", "2025-12-31", "2026-01-02"] };
      }
      return data;
    });
    fireEvent.click(screen.getByRole("button", { name: "5 個交易日" }));
    await waitFor(() => expect(api.getBrokerMetrics).toHaveBeenCalledWith(id, "proxy.broker_top_n_net_imbalance",
      { from: "2025-12-24", to: "2026-01-02" }, expect.anything(), expect.anything(), expect.any(AbortSignal)));
  });

  it("uses the exact saved daily date and custom dates, without counting available report rows", async () => {
    const range = { from: "2025-12-01", to: "2026-01-12" };
    boot(range); await screen.findByText("偏買超（Proxy）");
    expect(api.getBrokerTopBranches).toHaveBeenCalledWith(id, brokerRangeFixture, expect.not.objectContaining({ sessions: 1 }), expect.any(AbortSignal));
    fireEvent.click(screen.getByRole("button", { name: "自訂區間" }));
    await waitFor(() => expect(api.getBrokerTopBranches).toHaveBeenLastCalledWith(id, range, expect.anything(), expect.any(AbortSignal)));
    expect(screen.getByText(/使用上方籌碼日期範圍/)).toBeVisible();
  });

  it("requests a selected branch cumulative metric and retains financial precision", async () => {
    boot(); await screen.findByText("偏買超（Proxy）");
    fireEvent.change(screen.getByLabelText("指定分點"), { target: { value: brokerIds.branch } });
    await screen.findByText("123,456,789,012,345,678");
    expect(api.getBrokerMetrics).toHaveBeenCalledWith(id, "broker.branch_session_cumulative_net", brokerRangeFixture,
      { branch_id: brokerIds.branch, sessions: 1 }, expect.objectContaining({ trade_scope: brokerIds.scope, source: brokerIds.source }), expect.any(AbortSignal));
    vi.mocked(api.getBrokerMetrics).mockImplementation(async (_id, algorithm, range, parameters) => metric(algorithm, range, parameters));
    fireEvent.click(screen.getByRole("button", { name: "自訂區間" }));
    await waitFor(() => expect(api.getBrokerMetrics).toHaveBeenCalledWith(id, "broker.branch_range_net_quantity", brokerRangeFixture,
      { branch_id: brokerIds.branch }, expect.anything(), expect.any(AbortSignal)));
  });

  it("does not present old branch values under a newly selected branch", async () => {
    boot(); await screen.findByText("偏買超（Proxy）");
    fireEvent.change(screen.getByLabelText("指定分點"), { target: { value: brokerIds.branch } });
    await screen.findByText("123,456,789,012,345,678");
    vi.mocked(api.getBrokerMetrics).mockImplementation((_id, algorithm, range, parameters) =>
      algorithm === "broker.branch_session_cumulative_net" ? new Promise(() => {}) : Promise.resolve(metric(algorithm, range, parameters)));
    fireEvent.change(screen.getByLabelText("指定分點"), { target: { value: secondaryBranch } });
    expect(screen.queryByText("123,456,789,012,345,678")).not.toBeInTheDocument();
  });

  it("shows unavailable empty data without synthetic rankings or zero charts", async () => {
    vi.mocked(api.getBrokerHistory).mockImplementation(async (_id, range) => ({ ...brokerHistoryFixture(), ...range, items: [], count: 0 }));
    vi.mocked(api.getBrokerLatest).mockResolvedValue({ ...brokerLatestFixture(), point: null });
    boot(); await screen.findByText(/尚無已收錄券商分點資料/);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(api.getBrokerTopBranches).not.toHaveBeenCalled();
    expect(screen.queryByTestId("每日重選 TOP 15 分點 Proxy")).not.toBeInTheDocument();
  });

  it("handles loading and API failures with a retry, and never paints a stale previous scope", async () => {
    vi.mocked(api.getBrokerHistory).mockRejectedValueOnce(new ApiError(503, "api_unavailable", "資料庫尚未就緒"));
    vi.mocked(api.getBrokerLatest).mockRejectedValue(new ApiError(503, "api_unavailable", "資料庫尚未就緒"));
    boot(); expect(screen.getByText("正在載入分點資料…")).toBeVisible();
    await screen.findByRole("button", { name: "重新載入分點資料" });
    expect(screen.getAllByText("分點 API 暫時無法連線").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "重新載入分點資料" }));
    await screen.findByRole("table", { name: /TOP 15 買超分點/ });
    expect(api.getBrokerHistory).toHaveBeenCalledTimes(2);
  });

  it("keeps publication unknown and does not infer stale status from older observations", async () => {
    boot(); await screen.findByText("偏買超（Proxy）");
    expect(screen.getByText("未知，未自行推算")).toBeVisible();
    expect(screen.getByText("時效未核實")).toBeVisible();
    expect(screen.queryByText(/Stale ·/)).not.toBeInTheDocument();
    expect(screen.getByText(/發布時間未知 · Provisional/)).toBeVisible();
    expect(screen.getByText(/正式下載及公開／衍生展示授權尚未核實/)).toBeVisible();
  });

  it("preserves period and branch settings when a visited tab is hidden then shown", async () => {
    const view = boot(); await screen.findByText("偏買超（Proxy）");
    fireEvent.change(screen.getByLabelText("指定分點"), { target: { value: brokerIds.branch } });
    await screen.findByText("123,456,789,012,345,678");
    const requests = vi.mocked(api.getBrokerTopBranches).mock.calls.length;
    const chart = screen.getByTestId("每日重選 TOP 15 分點 Proxy");
    view.rerender(<BrokerFlowPanel id={id} range={brokerRangeFixture} active={false} currency="TWD"/>);
    view.rerender(<BrokerFlowPanel id={id} range={brokerRangeFixture} active currency="TWD"/>);
    expect(screen.getByLabelText("指定分點")).toHaveValue(brokerIds.branch);
    expect(screen.getByRole("button", { name: "1 日" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("每日重選 TOP 15 分點 Proxy")).toBe(chart);
    await waitFor(() => expect(api.getBrokerTopBranches).toHaveBeenCalledTimes(requests));
  });

  it("requires a single exact scope before aggregating ambiguous sources", async () => {
    const history = brokerHistoryFixture();
    const extra = structuredClone(history.items[0]); extra.scope.id = secondaryScope; extra.scope.code = "SYNTHETIC-block";
    vi.mocked(api.getBrokerHistory).mockResolvedValue({ ...history, items: [...history.items, extra], count: 2 });
    vi.mocked(api.getBrokerLatest).mockImplementation(async (_id, selection) => {
      if (!selection?.trade_scope) throw new ApiError(422, "ambiguous_trade_scope", "請選擇明確成交口徑");
      return { ...brokerLatestFixture(), point: selection.trade_scope === secondaryScope ? extra : history.items[0] };
    });
    boot(); await screen.findByText(/請明確選擇來源與成交口徑/);
    expect(api.getBrokerTopBranches).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("分點來源與成交口徑"), { target: { value: secondaryScope } });
    await waitFor(() => expect(api.getBrokerTopBranches).toHaveBeenCalledWith(id, expect.anything(), expect.objectContaining({ trade_scope: secondaryScope }), expect.any(AbortSignal)));
  });

  it("blocks a mixed-Revision ranking window and explicitly rechecks without resetting controls", async () => {
    let repaired = false;
    vi.mocked(api.getBrokerTopBranches).mockImplementation(async (_id, range, options) => {
      const response = top(range, options);
      if (!repaired && options?.side === "net_sellers") {
        response.result.input_refs[0].revision_id = "00000000-0000-0000-0000-000000000999";
      }
      return response;
    });
    boot();
    await screen.findByText("買超與賣超榜的輸入 Revision 不一致");
    expect(api.getBrokerMetrics).not.toHaveBeenCalled();
    expect(screen.queryByText("偏買超（Proxy）")).not.toBeInTheDocument();
    const selectedScope = screen.getByLabelText("分點來源與成交口徑");
    expect(selectedScope).toHaveValue(brokerIds.scope);
    repaired = true;
    fireEvent.click(screen.getByRole("button", { name: "重新核對本區間" }));
    await screen.findByText("偏買超（Proxy）");
    expect(screen.getByLabelText("分點來源與成交口徑")).toHaveValue(brokerIds.scope);
    expect(screen.getByRole("button", { name: "1 日" })).toHaveAttribute("aria-pressed", "true");
    expect(api.getBrokerTopBranches).toHaveBeenCalledTimes(4);
    expect(screen.queryByText("買超與賣超榜的輸入 Revision 不一致")).not.toBeInTheDocument();
  });

  it("does not blend revised Proxy and count values with an older ranking snapshot", async () => {
    vi.mocked(api.getBrokerMetrics).mockImplementation(async (_id, algorithm, range, parameters) => {
      const response = metric(algorithm, range, parameters);
      if (algorithm === "proxy.broker_top_n_net_imbalance" || algorithm === "broker.active_buy_sell_count_difference") {
        response.result.input_refs[0].revision_id = "00000000-0000-0000-0000-000000000998";
      }
      return response;
    });
    boot();
    await screen.findByText("Proxy 與排行榜的輸入 Revision 不一致");
    expect(screen.queryByText("偏買超（Proxy）")).not.toBeInTheDocument();
    const buyCard = screen.getByText("選中分點買進").closest("div")!;
    expect(within(buyCard).getByText("—")).toBeVisible();
    await screen.findByText("部分指標的輸入 Revision 已變更");
    const countCard = screen.getByText("最新觀察日：有買進減有賣出家數").closest("div")!;
    expect(within(countCard).getByText("—")).toBeVisible();
    expect(screen.getByTestId("分點買賣家數差")).not.toHaveTextContent('"value":"-1"');
    expect(screen.getByTestId("分點買賣家數差")).toHaveTextContent('"value":"0"');
    expect(screen.getByRole("table", { name: /TOP 15 買超分點/ })).toBeVisible();
  });
});
