/** Local browser checks against saved data only; never calls an exchange API. */
import { expect, test, type APIRequestContext } from "@playwright/test";
import type { Instrument, LatestResponse } from "../src/lib/market-types";

async function savedStock(request: APIRequestContext): Promise<Instrument> {
  const response = await request.get(
    "/api/market/instruments?q=2330&market=TWSE",
  );
  expect(
    response.ok(),
    "Start the frontend and FastAPI before browser checks",
  ).toBeTruthy();
  const { items } = (await response.json()) as { items: Instrument[] };
  const instrument = items.find(
    (item) => item.symbol === "2330" && item.instrument_type === "stock",
  );
  expect(
    instrument,
    "Browser acceptance requires the real 2330 instrument already saved by Phase 1B",
  ).toBeTruthy();
  return instrument!;
}

test("search 2330 and inspect real stored daily, weekly and monthly candles", async ({
  page,
  request,
}, testInfo) => {
  const instrument = await savedStock(request);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "尋找你的研究標的" }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("home-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "上市", exact: true }).click();
  const search = page.getByRole("combobox");
  await search.fill("2330");
  await expect(
    page.getByRole("option").filter({ hasText: instrument.symbol }),
  ).toBeVisible();
  await search.press("ArrowDown");
  await search.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/stocks/${instrument.id}$`));
  await expect(
    page.getByRole("heading", { name: new RegExp(instrument.name) }),
  ).toBeVisible();
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  const daily = await request.get(`/api/market/stocks/${instrument.id}/latest`);
  const latest = (await daily.json()) as LatestResponse;
  expect(
    latest.bar,
    "Acceptance needs at least one real saved 2330 bar",
  ).not.toBeNull();
  await expect(
    page.getByText(`所屬交易日 ${latest.bar!.trade_date.replaceAll("-", "/")}`),
  ).toBeVisible();
  const figure = page.getByLabel("日 K與成交量圖");
  expect(await figure.locator("canvas").count()).toBeGreaterThanOrEqual(4);
  const canvasBox = await figure.locator('[aria-hidden="true"]').boundingBox();
  expect(canvasBox).not.toBeNull();
  // Walk the actual chart until the existing candle receives the crosshair.
  for (let step = 1; step < 120; step++) {
    await page.mouse.move(
      canvasBox!.x + (canvasBox!.width * step) / 120,
      canvasBox!.y + 100,
    );
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
    if (await page.getByLabel("十字線行情").isVisible()) break;
  }
  await expect(page.getByLabel("十字線行情")).toBeVisible();
  await page.mouse.wheel(0, -100);
  await page.mouse.down();
  await page.mouse.move(
    canvasBox!.x + canvasBox!.width / 2 + 20,
    canvasBox!.y + 120,
    { steps: 4 },
  );
  await page.mouse.up();
  await page.screenshot({
    path: testInfo.outputPath("stock-desktop.png"),
    fullPage: true,
  });
  for (const [label, timeframe] of [
    ["週 K", "1w"],
    ["月 K", "1M"],
    ["日 K", "1d"],
  ]) {
    const response = page.waitForResponse(
      (response) =>
        response.url().includes("/bars?") &&
        new URL(response.url()).searchParams.get("timeframe") === timeframe,
    );
    await page.getByRole("button", { name: label, exact: true }).click();
    expect((await response).ok()).toBeTruthy();
    await expect(page.getByLabel(`${label}與成交量圖`)).toBeVisible();
    await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test("mobile name search, market selection and chart resizing", async ({
  page,
  request,
}, testInfo) => {
  const instrument = await savedStock(request);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("combobox").fill(instrument.name);
  await page.getByRole("option").filter({ hasText: instrument.symbol }).click();
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "月 K", exact: true }).click();
  await expect(page.getByLabel("月 K與成交量圖")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("stock-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 768, height: 1024 });
  await expect(page.getByLabel("月 K與成交量圖")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("API unavailable and empty states never create example candles", async ({
  page,
  request,
}) => {
  const instrument = await savedStock(request);
  const latestResponse = await request.get(
    `/api/market/stocks/${instrument.id}/latest`,
  );
  const latest = (await latestResponse.json()) as LatestResponse;
  await page.route("**/api/market/stocks/*/latest", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: { code: "api_unavailable" } },
    }),
  );
  await page.goto(`/stocks/${instrument.id}`);
  await expect(
    page.getByRole("heading", { name: "行情 API 暫時無法連線" }),
  ).toBeVisible();
  expect(await page.locator("canvas").count()).toBe(0);
  await page.unroute("**/api/market/stocks/*/latest");
  // Synthetic empty response exists only in this browser test; no DB writes.
  const noData = {
    ...latest,
    bar: null,
    history: {
      available_from: null,
      available_to: null,
      completeness: "not_verified",
    },
    freshness: {
      ...latest.freshness,
      assessment: "no_data",
      latest_trade_date: null,
      last_ingested_at: null,
      age_calendar_days: null,
    },
  };
  await page.route("**/api/market/stocks/*/latest", (route) =>
    route.fulfill({ json: noData }),
  );
  await page.route("**/api/market/stocks/*/bars?*", (route) =>
    route.fulfill({
      json: {
        ...noData,
        timeframe: "1d",
        from: latest.freshness.market_local_date,
        to: latest.freshness.market_local_date,
        bars: [],
        count: 0,
        query_limit: 1000,
      },
    }),
  );
  await page.getByRole("button", { name: "重新載入" }).click();
  await expect(
    page.getByText("此範圍沒有行情資料", { exact: true }),
  ).toBeVisible();
  expect(await page.locator("canvas").count()).toBe(0);
});
