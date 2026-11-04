import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getBars, getInstrument, getLatest, searchInstruments } from "../src/lib/market-api";
import { barsFixture, instrumentFixture, latestFixture } from "./fixtures/market-api";

afterEach(() => vi.unstubAllGlobals());
const respond = (body: unknown, status = 200) => vi.fn().mockResolvedValue(Response.json(body, { status }));

describe("market API client", () => {
  it("encodes a symbol/name query and market through the same-origin proxy", async () => {
    const fetcher = respond({ items: [instrumentFixture], total: 1, page: 1, page_size: 20, total_pages: 1 });
    vi.stubGlobal("fetch", fetcher);
    const result = await searchInstruments(" 測試 & 公司 ", "TWSE");
    expect(result.items[0]).toEqual(instrumentFixture);
    expect(fetcher).toHaveBeenCalledWith("/api/market/instruments?q=%E6%B8%AC%E8%A9%A6+%26+%E5%85%AC%E5%8F%B8&page_size=20&market=TWSE", expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }));
  });
  it("omits market filtering for the ALL selection", async () => {
    const fetcher = respond({ items: [], total: 0, page: 1, page_size: 20, total_pages: 0 });
    vi.stubGlobal("fetch", fetcher);
    expect((await searchInstruments("unknown", "ALL")).items).toEqual([]);
    expect(fetcher.mock.calls[0][0]).not.toContain("market=");
  });
  it("reads instrument and latest while preserving financial decimal strings", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json(instrumentFixture)).mockResolvedValueOnce(Response.json(latestFixture));
    vi.stubGlobal("fetch", fetcher);
    expect(await getInstrument(instrumentFixture.id)).toEqual(instrumentFixture);
    const latest = await getLatest(instrumentFixture.id);
    expect(latest.bar?.close).toBe("12.50000000");
    expect(latest.bar?.change_percent).toBeNull();
  });
  it.each(["1d", "1w", "1M"] as const)("requests backend %s aggregation and a bounded explicit range", async timeframe => {
    const fetcher = respond({ ...barsFixture, timeframe });
    vi.stubGlobal("fetch", fetcher);
    expect((await getBars(instrumentFixture.id, timeframe, undefined, { from: "2026-01-01", to: "2026-01-31" })).timeframe).toBe(timeframe);
    expect(fetcher.mock.calls[0][0]).toBe(`/api/market/stocks/${instrumentFixture.id}/bars?timeframe=${timeframe}&from=2026-01-01&to=2026-01-31`);
  });
  it("accepts an existing instrument without bars", async () => {
    vi.stubGlobal("fetch", respond({ ...barsFixture, count: 0, bars: [] }));
    expect((await getBars(instrumentFixture.id, "1d")).bars).toEqual([]);
    vi.stubGlobal("fetch", respond({ ...latestFixture, bar: null }));
    expect((await getLatest(instrumentFixture.id)).bar).toBeNull();
  });
  it("preserves valid Pydantic Decimal exponent notation", async () => {
    vi.stubGlobal("fetch", respond({ ...latestFixture, bar: { ...latestFixture.bar, turnover: "0E-8" } }));
    expect((await getLatest(instrumentFixture.id)).bar?.turnover).toBe("0E-8");
  });
  it.each([404, 422, 503])("maps HTTP %s to a typed safe error", async status => {
    vi.stubGlobal("fetch", respond({ detail: { code: "stock_not_found", message: "secret upstream message" } }, status));
    try { await getLatest(instrumentFixture.id); expect.fail("must reject"); }
    catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ status, code: "stock_not_found" });
      expect((error as Error).message).not.toContain("secret");
    }
  });
  it.each([new TypeError("offline"), new DOMException("timeout", "TimeoutError")])("handles unavailable APIs", async error => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
    await expect(getLatest(instrumentFixture.id)).rejects.toMatchObject({ status: 503, code: "api_unavailable" });
  });
  it("preserves caller cancellation rather than treating it as an outage", async () => {
    const controller = new AbortController(); controller.abort();
    const abort = new DOMException("cancelled", "AbortError");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(abort));
    await expect(getLatest(instrumentFixture.id, controller.signal)).rejects.toBe(abort);
  });
  it.each([
    {},
    { ...barsFixture, count: 2 },
    { ...barsFixture, bars: [{ ...barsFixture.bars[0], volume_shares: Number.MAX_SAFE_INTEGER + 1 }] },
    { ...barsFixture, bars: [{ ...barsFixture.bars[0], close: 12.5 }] },
    { ...barsFixture, bars: [{ ...barsFixture.bars[0], trade_date: "2026-02-30" }] },
  ])("refuses unsafe or incompatible responses", async body => {
    vi.stubGlobal("fetch", respond(body));
    await expect(getBars(instrumentFixture.id, "1d")).rejects.toMatchObject({ status: 502, code: "invalid_response" });
  });
  it("handles an upstream non-JSON response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("upstream HTML", { status: 503 })));
    await expect(getLatest(instrumentFixture.id)).rejects.toMatchObject({ status: 503, code: "invalid_response" });
  });
});
