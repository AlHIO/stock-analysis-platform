/** Live local API acceptance, using only canonical data already saved by Phase1B. */
import { expect, test } from "@playwright/test";
import type { Instrument } from "../src/lib/market-types";
import type { IndicatorResponse } from "../src/lib/technical-types";
import { decimal } from "../src/lib/format";

test("real saved indicators: settings, warmup panes, timeframe and mobile", async ({
  page,
  request,
}, testInfo) => {
  const response = await request.get(
    "/api/market/instruments?q=2330&market=TWSE",
  );
  expect(response.ok()).toBeTruthy();
  const instruments = (await response.json()) as { items: Instrument[] };
  const instrument = instruments.items.find(
    (item) => item.symbol === "2330" && item.instrument_type === "stock",
  );
  expect(
    instrument,
    "The real 2330 instrument must already be saved before browser acceptance",
  ).toBeTruthy();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/stocks/${instrument!.id}`);
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await page.getByText("指標參數", { exact: true }).click();
  await page.getByLabel("SMA 期數", { exact: true }).fill("1");
  await page.getByLabel("EMA 期數", { exact: true }).fill("1");
  await page.getByLabel("成交量均線 期數", { exact: true }).fill("1");
  await page.getByRole("button", { name: "套用參數" }).click();
  for (const name of ["SMA", "EMA", "RSI", "MACD", "成交量均線"]) {
    const loaded = page.waitForResponse(
      (response) =>
        response.url().includes("/indicators?") &&
        response.request().method() === "GET",
    );
    await page.getByRole("checkbox", { name: new RegExp(`^${name}`) }).check();
    const reply = await loaded;
    expect(reply.ok()).toBeTruthy();
  }
  const legend = page.getByLabel("已顯示技術指標");
  await expect(legend).toContainText("SMA(1)");
  await expect(legend).toContainText("EMA(1)");
  await expect(legend).toContainText("RSI(14)");
  await expect(legend).toContainText("MACD(12,26,9)");
  await expect(legend).toContainText("VOL MA(1)");
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  expect(
    await page.getByLabel("日 K與成交量圖").locator("canvas").count(),
  ).toBeGreaterThanOrEqual(8);
  // Assert the numerical text comes from the real backend, not a frontend formula.
  const actual = await request.get(
    `/api/market/stocks/${instrument!.id}/indicators?indicator=sma&parameters=%7B%22period%22%3A1%7D`,
  );
  expect(actual.ok()).toBeTruthy();
  const computed = (await actual.json()) as IndicatorResponse;
  expect(computed.points.at(-1)?.values.sma).not.toBeNull();
  await expect(legend).toContainText(
    decimal(computed.points.at(-1)!.values.sma),
  );
  await page.screenshot({
    path: testInfo.outputPath("technical-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "週 K", exact: true }).click();
  await expect(page.getByLabel("週 K與成交量圖")).toBeVisible();
  await expect(legend).toContainText("SMA(1)");
  await expect(page.getByRole("checkbox", { name: /^MACD/ })).toBeChecked();
  await page.getByRole("button", { name: "月 K", exact: true }).click();
  await expect(page.getByLabel("月 K與成交量圖")).toBeVisible();
  await expect(legend).toContainText("MACD(12,26,9)");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("technical-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("checkbox", { name: /^RSI/ }).uncheck();
  await expect(legend).not.toContainText("RSI(14)");
  expect(errors).toEqual([]);
});

test("indicator API failure preserves candles and retries independently", async ({
  page,
  request,
}) => {
  const instruments = (await (
    await request.get("/api/market/instruments?q=2330&market=TWSE")
  ).json()) as { items: Instrument[] };
  const instrument = instruments.items.find(
    (item) => item.symbol === "2330" && item.instrument_type === "stock",
  );
  expect(instrument).toBeTruthy();
  await page.route("**/api/market/stocks/*/indicators?*", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: { code: "api_unavailable" } },
    }),
  );
  await page.goto(`/stocks/${instrument!.id}`);
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await page.getByRole("checkbox", { name: /^SMA/ }).check();
  const controls = page.getByLabel("技術指標設定");
  await expect(controls.getByRole("alert")).toContainText("SMA 暫時無法載入");
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await page.unroute("**/api/market/stocks/*/indicators?*");
  await page.getByRole("button", { name: "重試指標" }).click();
  await expect(page.getByLabel("已顯示技術指標")).toContainText("SMA(20)");
  await expect(controls.getByRole("alert")).toHaveCount(0);
});
