import { afterEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";
import { GET } from "../src/app/api/market/[...path]/route";
import { getIndicator } from "../src/lib/technical-api";
import { isIndicatorResponse } from "../src/lib/technical-types";
import { indicatorFixture } from "./fixtures/technical";
import { instrumentFixture } from "./fixtures/market-api";
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
const id = instrumentFixture.id;
const range = { from: "2026-01-05", to: "2026-01-05" };

describe("technical API client and guards", () => {
  it.each([
    { timeframe: "1w" },
    {
      instrument: {
        ...instrumentFixture,
        id: "00000000-0000-0000-0000-000000000099",
      },
    },
    { parameters: { period: 3 } },
  ])(
    "rejects a successful response belonging to different request inputs %j",
    async (patch) => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            Response.json({ ...indicatorFixture(), ...patch }),
          ),
      );
      await expect(
        getIndicator(id, "1d", "sma", { period: 20 }, range),
      ).rejects.toMatchObject({ status: 502, code: "invalid_response" });
    },
  );
  it("requests configured parameters and raw prices through the same-origin proxy", async () => {
    const body = indicatorFixture();
    const fetcher = vi.fn().mockResolvedValue(Response.json(body));
    vi.stubGlobal("fetch", fetcher);
    expect(await getIndicator(id, "1d", "sma", { period: 20 }, range)).toEqual(
      body,
    );
    const url = new URL(fetcher.mock.calls[0][0], "http://localhost");
    expect(url.pathname).toBe(`/api/market/stocks/${id}/indicators`);
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      indicator: "sma",
      timeframe: "1d",
      parameters: '{"period":20}',
      price_basis: "raw",
      ...range,
    });
  });
  it("preserves Decimal text and explicit warmup nulls", () => {
    const response = indicatorFixture("macd");
    response.points[0] = {
      ...response.points[0],
      status: "warmup",
      values: { macd: "0E-12", signal: null, histogram: null },
    };
    expect(isIndicatorResponse(response)).toBe(true);
  });
  it.each([
    { algorithm_version: "2.0.0" },
    { price_basis: "adjusted" },
    { count: 2 },
    {
      points: [
        {
          trade_date: "2026-01-05",
          values: { sma: 12.5 },
          status: "ready",
          is_provisional: false,
        },
      ],
    },
    { parameters: { period: 0 } },
    { parameters: { period: true } },
    { parameters: { period: 20, extra: 5 } },
  ])("rejects unsafe or incompatible response %j", (patch) => {
    expect(isIndicatorResponse({ ...indicatorFixture(), ...patch })).toBe(
      false,
    );
  });
  it("rejects duplicate dates and missing numeric keys", () => {
    const response = indicatorFixture();
    expect(
      isIndicatorResponse({
        ...response,
        count: 2,
        points: [...response.points, ...response.points],
      }),
    ).toBe(false);
    expect(
      isIndicatorResponse({
        ...response,
        points: [{ ...response.points[0], values: { wrong: "1" } }],
      }),
    ).toBe(false);
  });
  it("returns safe errors on API failure without parsing a fake indicator", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            detail: {
              code: "adjusted_price_unavailable",
              message: "private",
            },
          },
          { status: 422 },
        ),
      ),
    );
    await expect(
      getIndicator(id, "1d", "sma", { period: 20 }, range),
    ).rejects.toMatchObject({
      status: 422,
      code: "adjusted_price_unavailable",
    });
  });
  it("allows only documented indicator query parameters in proxy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(indicatorFixture())),
    );
    const path = ["stocks", id, "indicators"];
    const call = (query: string) =>
      GET(
        new Request(
          `http://localhost/api/market/${path.join("/")}?${query}`,
        ) as NextRequest,
        { params: Promise.resolve({ path }) },
      );
    expect(
      (
        await call(
          "indicator=sma&parameters=%7B%22period%22%3A20%7D&timeframe=1d&price_basis=raw&from=2026-01-05&to=2026-01-05",
        )
      ).status,
    ).toBe(200);
    expect(
      (await call("indicator=sma&source_url=https://example.com")).status,
    ).toBe(422);
    expect((await call("indicator=sma&indicator=ema")).status).toBe(422);
  });
});
