import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StockSearch } from "../src/components/stock-search";
import { searchInstruments } from "../src/lib/market-api";
import type { Instrument, InstrumentPage } from "../src/lib/market-types";

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a {...props}>{children}</a> }));
vi.mock("../src/lib/market-api", () => ({
  searchInstruments: vi.fn(),
  ApiError: class extends Error { status = 503; },
}));

// Explicitly fictional test fixtures; never imported by application code.
const stock: Instrument = {
  id: "00000000-0000-4000-8000-000000000001", symbol: "2330", name: "測試公司",
  market: { id: "00000000-0000-4000-8000-000000000002", code: "TWSE", name: "測試上市市場", timezone: "Asia/Taipei", currency: "TWD" },
  instrument_type: "stock", status: "active", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const page = (items: Instrument[] = [stock]): InstrumentPage => ({ items, total: items.length, page: 1, page_size: 20, total_pages: items.length ? 1 : 0 });
const search = vi.mocked(searchInstruments);
async function debounce() { await act(async () => { await vi.advanceTimersByTimeAsync(250); }); }
function enter(value: string) { fireEvent.change(screen.getByRole("combobox"), { target: { value } }); }

beforeEach(() => { vi.useFakeTimers(); search.mockResolvedValue(page()); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); });

describe("StockSearch", () => {
  it("debounces symbol queries and links suggestions by instrument UUID", async () => {
    render(<StockSearch />);
    enter(" 2330 ");
    expect(screen.getByRole("status")).toHaveTextContent("搜尋中");
    expect(search).not.toHaveBeenCalled();
    await debounce();
    expect(search).toHaveBeenCalledWith("2330", "", expect.any(AbortSignal), 1);
    expect(screen.getByRole("option")).toHaveAttribute("href", `/stocks/${stock.id}`);
    expect(screen.getByRole("option")).toHaveTextContent("測試公司");
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-expanded", "true");
  });

  it("searches names after Chinese IME composition ends", async () => {
    render(<StockSearch />);
    const input = screen.getByRole("combobox");
    fireEvent.compositionStart(input);
    enter("測試公司");
    await debounce();
    expect(search).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    await debounce();
    expect(search).toHaveBeenCalledWith("測試公司", "", expect.any(AbortSignal), 1);
  });

  it("passes the selected TWSE or TPEX market and searches again when changed", async () => {
    render(<StockSearch compact />);
    fireEvent.click(screen.getByRole("button", { name: "上市" }));
    enter("2330");
    await debounce();
    expect(search).toHaveBeenLastCalledWith("2330", "TWSE", expect.any(AbortSignal), 1);
    fireEvent.click(screen.getByRole("button", { name: "上櫃" }));
    await debounce();
    expect(search).toHaveBeenLastCalledWith("2330", "TPEX", expect.any(AbortSignal), 1);
    expect(screen.getByRole("button", { name: "上櫃" })).toHaveAttribute("aria-pressed", "true");
  });

  it("explains API unavailability and allows retry without changing the query", async () => {
    search.mockRejectedValueOnce(new Error("network unavailable"));
    render(<StockSearch />);
    enter("2330");
    await debounce();
    expect(screen.getByRole("alert")).toHaveTextContent("API 暫時無法連線");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新搜尋" }));
    await debounce();
    expect(screen.getByRole("option")).toHaveTextContent("測試公司");
  });

  it("shows an honest empty state and never creates suggestions", async () => {
    search.mockResolvedValue(page([]));
    render(<StockSearch />);
    enter("不存在");
    await debounce();
    expect(screen.getByRole("status")).toHaveTextContent("找不到符合條件的股票");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("paginates past a non-stock page without falsely reporting no matching stocks", async () => {
    const first: InstrumentPage = { items: [{ ...stock, instrument_type: "etf" }], total: 21, page: 1, page_size: 20, total_pages: 2 };
    const second: InstrumentPage = { items: [stock], total: 21, page: 2, page_size: 20, total_pages: 2 };
    search.mockResolvedValueOnce(first).mockResolvedValueOnce(second).mockResolvedValueOnce(first);
    render(<StockSearch />);
    enter("測試");
    await debounce();
    expect(screen.getByRole("status")).toHaveTextContent("本頁沒有股票");
    expect(screen.getByRole("status")).not.toHaveTextContent("找不到符合條件");
    expect(screen.getByRole("button", { name: "上一頁" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一頁" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    expect(screen.getByRole("status")).toHaveTextContent("搜尋中");
    await debounce();
    expect(search).toHaveBeenLastCalledWith("測試", "", expect.any(AbortSignal), 2);
    expect(screen.getByRole("option")).toHaveTextContent("測試公司");
    expect(screen.getByText("第 2 / 2 頁")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一頁" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "上一頁" }));
    await debounce();
    expect(search).toHaveBeenLastCalledWith("測試", "", expect.any(AbortSignal), 1);
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("resets pagination for a new query and ignores the superseded page response", async () => {
    let resolvePage!: (value: InstrumentPage) => void;
    search.mockResolvedValueOnce({ ...page(), total: 21, total_pages: 2 })
      .mockImplementationOnce(() => new Promise(resolve => { resolvePage = resolve; }))
      .mockResolvedValueOnce(page([{ ...stock, name: "新查詢公司" }]));
    render(<StockSearch />);
    enter("舊查詢"); await debounce();
    fireEvent.click(screen.getByRole("button", { name: "下一頁" })); await debounce();
    const secondPageSignal = search.mock.calls[1][2];
    enter("新查詢");
    expect(secondPageSignal?.aborted).toBe(true);
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    await debounce();
    expect(search).toHaveBeenLastCalledWith("新查詢", "", expect.any(AbortSignal), 1);
    await act(async () => { resolvePage({ ...page([{ ...stock, name: "過時分頁公司" }]), page: 2 }); });
    expect(screen.getByRole("option")).toHaveTextContent("新查詢公司");
    expect(screen.queryByText("過時分頁公司")).not.toBeInTheDocument();
  });

  it("resets pagination when changing the market", async () => {
    search.mockResolvedValue({ ...page(), total: 21, total_pages: 2 });
    render(<StockSearch />);
    enter("測試"); await debounce();
    fireEvent.click(screen.getByRole("button", { name: "下一頁" })); await debounce();
    expect(search).toHaveBeenLastCalledWith("測試", "", expect.any(AbortSignal), 2);
    fireEvent.click(screen.getByRole("button", { name: "上櫃" }));
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
    await debounce();
    expect(search).toHaveBeenLastCalledWith("測試", "TPEX", expect.any(AbortSignal), 1);
  });

  it("cannot keyboard-select an old suggestion while the query is changing", async () => {
    render(<StockSearch />);
    enter("原查詢"); await debounce();
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "ArrowDown" });
    enter("新查詢");
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(router.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("does not route non-stock instruments to the stocks API", async () => {
    search.mockResolvedValue(page([{ ...stock, instrument_type: "etf" }]));
    render(<StockSearch />);
    enter("2330");
    await debounce();
    expect(screen.getByRole("status")).toHaveTextContent("本頁沒有股票");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("aborts superseded requests and ignores responses that arrive out of order", async () => {
    let resolveFirst!: (result: InstrumentPage) => void;
    search.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    render(<StockSearch />);
    enter("舊查詢");
    await debounce();
    const oldSignal = search.mock.calls[0][2];
    enter("新查詢");
    expect(oldSignal?.aborted).toBe(true);
    await debounce();
    expect(screen.getByRole("option")).toHaveTextContent("測試公司");
    await act(async () => { resolveFirst(page([{ ...stock, name: "過時結果" }])); });
    expect(screen.getByRole("option")).toHaveTextContent("測試公司");
    expect(screen.queryByText("過時結果")).not.toBeInTheDocument();
  });

  it("supports keyboard selection and Escape closes suggestions", async () => {
    render(<StockSearch />);
    enter("2330");
    await debounce();
    const input = screen.getByRole("combobox");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getByRole("option")).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", screen.getByRole("option").id);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(router.push).toHaveBeenCalledWith(`/stocks/${stock.id}`);
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("never requests an empty or whitespace query", async () => {
    render(<StockSearch />);
    enter("   ");
    await debounce();
    expect(search).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
