/** Local browser checks against saved data only; never calls an exchange API. */
import { expect, test, type APIRequestContext } from "@playwright/test";
import type {
  BarsResponse,
  Instrument,
  LatestResponse,
} from "../src/lib/market-types";
import type { IndicatorResponse } from "../src/lib/technical-types";
import { decimal } from "../src/lib/format";

async function savedStock(request: APIRequestContext): Promise<Instrument> {
  const response = await request.get(
    "/api/market/instruments?q=2330&market=TWSE&instrument_type=stock",
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
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
    );
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

test("custom historical dates read the real saved 2016 candle and retain the range across timeframes", async ({
  page,
  request,
}, testInfo) => {
  const instrument = await savedStock(request);
  const saved = await request.get(
    `/api/market/stocks/${instrument.id}/bars?timeframe=1d&from=2016-11-07&to=2016-11-07`,
  );
  expect(saved.ok()).toBeTruthy();
  const stored = (await saved.json()) as BarsResponse;
  expect(
    stored.bars.length,
    "The historical importer must already have saved the real 2016-11-07 bar",
  ).toBeGreaterThan(0);
  expect(stored.bars[0].trade_date).toBe("2016-11-07");

  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/stocks/${instrument.id}`);
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await page.getByLabel("開始日期", { exact: true }).fill("2016-11-07");
  await page.getByLabel("結束日期", { exact: true }).fill("2016-11-07");
  const daily = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname.endsWith(`/stocks/${instrument.id}/bars`) &&
      url.searchParams.get("timeframe") === "1d" &&
      url.searchParams.get("from") === "2016-11-07" &&
      url.searchParams.get("to") === "2016-11-07"
    );
  });
  await page.getByRole("button", { name: "套用日期", exact: true }).click();
  const response = await daily;
  expect(response.ok()).toBeTruthy();
  const displayed = (await response.json()) as BarsResponse;
  expect(displayed.bars).toEqual(stored.bars);
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText(/查詢範圍 2016\/11\/07 — 2016\/11\/07/),
  ).toBeVisible();
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();

  for (const [label, timeframe, from] of [
    ["週 K", "1w", "2016-11-07"],
    ["月 K", "1M", "2016-11-01"],
  ]) {
    const reply = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname.endsWith(`/stocks/${instrument.id}/bars`) &&
        url.searchParams.get("timeframe") === timeframe &&
        url.searchParams.get("from") === from &&
        url.searchParams.get("to") === "2016-11-07"
      );
    });
    await page.getByRole("button", { name: label, exact: true }).click();
    expect((await reply).ok()).toBeTruthy();
    await expect(page.getByLabel(`${label}與成交量圖`)).toBeVisible();
    await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  }
  await page.getByText("指標參數", { exact: true }).click();
  await page.getByLabel("SMA 期數", { exact: true }).fill("1");
  await page.getByRole("button", { name: "套用參數", exact: true }).click();
  const indicatorReply = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname.endsWith(`/stocks/${instrument.id}/indicators`) &&
      url.searchParams.get("indicator") === "sma" &&
      url.searchParams.get("timeframe") === "1M" &&
      url.searchParams.get("from") === "2016-11-01" &&
      url.searchParams.get("to") === "2016-11-07"
    );
  });
  await page.getByRole("checkbox", { name: /^SMA/ }).check();
  const calculated = await indicatorReply;
  expect(calculated.ok()).toBeTruthy();
  const indicator = (await calculated.json()) as IndicatorResponse;
  expect(indicator.points.at(-1)?.values.sma).not.toBeNull();
  expect(decimal(indicator.points.at(-1)!.values.sma)).toBe(
    decimal(stored.bars[0].close),
  );
  await expect(page.getByLabel("已顯示技術指標")).toContainText("SMA(1)");
  await expect(page.getByLabel("已顯示技術指標")).toContainText(
    decimal(stored.bars[0].close),
  );
  await page.screenshot({
    path: testInfo.outputPath("stock-historical-range.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("long observed date gaps are disclosed and recent segment keeps genuine stored data", async ({
  page,
  request,
}, testInfo) => {
  const instrument = await savedStock(request);
  await page.goto(`/stocks/${instrument.id}`);
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  const loaded = page.waitForResponse(
    (response) =>
      response.url().includes("/bars?") &&
      new URL(response.url()).searchParams.get("timeframe") === "1w",
  );
  await page.getByRole("button", { name: "最近 10 年", exact: true }).click();
  const response = await loaded;
  expect(response.ok()).toBeTruthy();
  const history = (await response.json()) as BarsResponse;
  await expect(page.getByLabel("週 K與成交量圖")).toBeVisible();
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  const gaps = history.bars.slice(1).flatMap((bar, index) => {
    const previous = history.bars[index];
    const days =
      (Date.parse(bar.first_trade_date) -
        Date.parse(previous.last_trade_date)) /
      86_400_000;
    return days > 90
      ? [{ previous: previous.last_trade_date, next: bar.first_trade_date }]
      : [];
  });
  const notice = page.getByRole("status", { name: "歷史資料日期間隔" });
  if (gaps.length) {
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("圖表按已收錄 K 棒排列");
    await expect(notice).toContainText(gaps[0].previous.replaceAll("-", "/"));
    await expect(notice).toContainText(gaps[0].next.replaceAll("-", "/"));
    await page.screenshot({
      path: testInfo.outputPath("historical-gap-notice.png"),
      fullPage: true,
    });
    const recent = page.waitForResponse(
      (reply) =>
        reply.url().includes("/bars?") && reply.request().method() === "GET",
    );
    await page
      .getByRole("button", { name: "只看最近已收錄區段", exact: true })
      .click();
    const recentResponse = await recent;
    expect(recentResponse.ok()).toBeTruthy();
    const recentBars = (await recentResponse.json()) as BarsResponse;
    expect(recentBars.to).toBe(history.to);
    expect(recentBars.from > history.from).toBeTruthy();
    expect(recentBars.bars.length).toBeGreaterThan(0);
    await expect(
      page.getByRole("button", { name: "週 K", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(notice).toHaveCount(0);
    await expect(page.getByLabel("週 K與成交量圖")).toBeVisible();
    // The action changes the query only: the earliest real observation remains saved.
    const latest = (await (
      await request.get(`/api/market/stocks/${instrument.id}/latest`)
    ).json()) as LatestResponse;
    expect(latest.history.available_from).toBe(history.history.available_from);
  } else {
    // The live backfill may eventually close this large gap; don't require a
    // deliberately incomplete database forever for browser acceptance.
    await expect(notice).toHaveCount(0);
  }
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
