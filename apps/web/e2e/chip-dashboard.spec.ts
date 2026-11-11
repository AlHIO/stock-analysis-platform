/**
 * SYNTHETIC browser-only responses. No exchanges, real quote claims, database
 * seeds or production writes. Browser requests are intercepted before proxy.
 */
import { expect, test, type Page } from "@playwright/test";
import {
  chipBarsFixture, chipInstrumentFixture, chipLatestFixture,
  institutionalHistoryFixture, institutionalMetricsFixture,
  ownershipHistoryFixture, ownershipMetricsFixture,
  marginHistoryFixture, lendingHistoryFixture,
} from "../tests/fixtures/chip";
import { indicatorFixture } from "../tests/fixtures/technical";
import type { IndicatorName } from "../src/lib/technical-types";

type Mode = "ready" | "loading" | "error" | "empty" | "partial" | "stale";
const stockPath = `/stocks/${chipInstrumentFixture.id}`;

function scoped(body: object, url: URL) {
  const result = structuredClone(body) as Record<string, unknown>;
  const from = url.searchParams.get("from") ?? "2025-10-25";
  const to = url.searchParams.get("to") ?? "2026-01-23";
  result.from = from;
  result.to = to;
  if (Array.isArray(result.items)) {
    const items = result.items.filter((point) => {
      const day = point.trade_date ?? point.as_of_date;
      return day >= from && day <= to;
    });
    result.items = items;
    result.count = items.length;
  }
  return result;
}

async function intercept(page: Page, mode: Mode = "ready") {
  const external: string[] = [];
  const errors: string[] = [];
  const chipRequests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (/https?:\/\/[^/]*(?:twse\.com\.tw|tpex\.org\.tw|tdcc\.com\.tw)/.test(request.url())) external.push(request.url());
  });
  await page.route(`**/api/market/stocks/${chipInstrumentFixture.id}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/latest")) return route.fulfill({ json: chipLatestFixture });
    if (url.pathname.endsWith("/bars")) return route.fulfill({ json: {
      ...chipBarsFixture, from: url.searchParams.get("from"), to: url.searchParams.get("to"),
      timeframe: url.searchParams.get("timeframe"),
    } });
    if (url.pathname.endsWith("/indicators")) {
      const name = url.searchParams.get("indicator") as IndicatorName;
      const parameters = JSON.parse(url.searchParams.get("parameters") ?? "{}");
      return route.fulfill({ json: indicatorFixture(name, {
        instrument: chipInstrumentFixture, freshness: chipLatestFixture.freshness,
        history: chipLatestFixture.history, parameters, from: url.searchParams.get("from")!, to: url.searchParams.get("to")!,
        count: chipBarsFixture.bars.length, calculation: { seed_policy: "full_available_history", history_from: chipBarsFixture.bars[0].trade_date, input_count: chipBarsFixture.bars.length, prehistory_count: 0, missing_policy: "reset_on_explicit_missing", calendar_policy: "observed_sessions_with_known_gaps", history_completeness: "not_verified" }, points: chipBarsFixture.bars.map((bar) => ({
          trade_date: bar.trade_date, values: { [name]: null }, status: "warmup", is_provisional: true,
        })),
      }) });
    }
    chipRequests.push(route.request().url());
    if (mode === "loading") {
      await new Promise<void>((resolve) => setTimeout(resolve, 2000));
    }
    if (mode === "error") return route.fulfill({ status: 503, json: {
      detail: { code: "database_unavailable", message: "SYNTHETIC outage", errors: null },
    } });
    let raw: object;
    if (url.pathname.endsWith("/institutional/metrics")) raw = institutionalMetricsFixture(url.searchParams.get("category") ?? "foreign_ex_dealers");
    else if (url.pathname.endsWith("/institutional")) raw = institutionalHistoryFixture();
    else if (url.pathname.endsWith("/ownership/metrics")) {
      const ownership = ownershipMetricsFixture();
      ownership.threshold = url.searchParams.get("threshold") ?? "400001";
      raw = ownership;
    } else if (url.pathname.endsWith("/ownership")) raw = ownershipHistoryFixture();
    else if (url.pathname.endsWith("/margin")) raw = marginHistoryFixture();
    else if (url.pathname.endsWith("/lending")) raw = lendingHistoryFixture(
      url.searchParams.get("report_kind") === "borrowed_positions" ? "borrowed_positions" : "lending_short_sales",
    );
    else return route.fulfill({ status: 404, json: { detail: { code: "not_found", message: "SYNTHETIC unsupported" } } });
    const body = scoped(raw, url);
    if (mode === "empty") {
      if (Array.isArray(body.items)) { body.items = []; body.count = 0; }
      else {
        body.daily = null;
        body.cumulative = (body.cumulative as Record<string, unknown>[]).map((entry) => ({ ...entry, value: null, status: "insufficient_history" }));
        body.streak = { ...(body.streak as object), status: "missing_data", buy_count: null, sell_count: null };
      }
    }
    if (mode === "partial") {
      body.coverage = { ...(body.coverage as object), completeness: "partial" };
      if (Array.isArray(body.items)) body.items = body.items.map((point) => ({ ...point,
        quality: { ...point.quality, status: "partial", domain_completeness: "partial", missing_components: ["dealer_hedging"], unavailable_reasons: ["SYNTHETIC missing provider field"] },
        ...(Array.isArray(point.components) ? {
          components: point.components.filter((component: { category: string }) => component.category !== "dealer_hedging"),
          derived_daily: point.derived_daily.map((metric: { category: string }) => metric.category === "dealer_total"
            ? { ...metric, value: null, status: "missing_data", reasons: ["missing_component"] } : metric),
        } : {}),
      }));
    }
    if (mode === "stale") body.freshness = { ...(body.freshness as object), assessment: "stale", age_calendar_days: 20 };
    return route.fulfill({ json: body });
  });
  return { external, errors, chipRequests };
}

test("desktop chip tabs retain price chart and indicator selection with canonical units", async ({ page }, testInfo) => {
  const checks = await intercept(page);
  await page.goto(stockPath);
  await expect(page.getByRole("tablist", { name: "籌碼分析" })).toBeVisible();
  const price = page.getByLabel("日 K與成交量圖");
  await expect(price).toBeVisible();
  await expect(price.locator("canvas").first()).toBeVisible();
  await page.getByRole("checkbox", { name: /^SMA/ }).check();
  await expect(page.getByRole("checkbox", { name: /^SMA/ })).toBeChecked();
  await price.locator("canvas").first().evaluate((element) => element.setAttribute("data-e2e-retained", "yes"));
  const institutionalChart = page.getByRole("img", { name: /^法人每日買賣超/ });
  await expect(institutionalChart).toHaveAttribute("data-chart-ready", "true");
  await institutionalChart.locator("canvas").first().evaluate((element) => element.setAttribute("data-e2e-retained", "yes"));
  for (const [tab, chart] of [
    ["集保持股", /^大戶持股比例/], ["融資融券", /^融資餘額/], ["借券賣出", /^借券賣出餘額/], ["三大法人", /^法人每日買賣超/],
  ] as const) {
    await page.getByRole("tab", { name: tab, exact: true }).click();
    await expect(page.getByRole("img", { name: chart })).toHaveAttribute("data-chart-ready", "true");
    await expect(page.getByRole("checkbox", { name: /^SMA/ })).toBeChecked();
    await expect(price.locator("canvas[data-e2e-retained=yes]")).toHaveCount(1);
  }
  await expect(institutionalChart.locator("canvas[data-e2e-retained=yes]")).toHaveCount(1);
  await expect(page.getByText("SYNTHETIC 合成測試來源（非真實籌碼）").first()).toBeVisible();
  expect(checks.external).toEqual([]);
  expect(checks.errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("chip-desktop.png"), fullPage: true });
});

test("weekly holdings remain weekly and date controls query same-origin saved APIs", async ({ page }, testInfo) => {
  const checks = await intercept(page);
  await page.goto(stockPath);
  await page.getByRole("tab", { name: "集保持股", exact: true }).click();
  const chart = page.getByRole("img", { name: /^大戶持股比例，每週統計/ });
  await expect(chart).toHaveAttribute("data-chart-ready", "true");
  await expect(page.getByText("僅使用來源真正的週統計日期，不建立每日持股點。", { exact: false }).first()).toBeVisible();
  await expect(page.getByText(/7000000|7,000,000/).first()).toBeVisible();
  await page.getByRole("combobox", { name: /級距分布統計日/ }).selectOption("2026-01-09");
  const distribution = page.getByRole("table", { name: /持股級距分布/ });
  await expect(distribution).toHaveAccessibleName("2026/01/09 持股級距分布 · 5 個來源級距");
  await expect(distribution.getByRole("cell", { name: "6,000,000", exact: true })).toBeVisible();
  const request = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname.endsWith("/ownership") && url.searchParams.get("from") === "2026-01-12" && url.searchParams.get("to") === "2026-01-19";
  });
  await page.getByLabel("籌碼開始日期", { exact: true }).fill("2026-01-12");
  await page.getByLabel("籌碼結束日期", { exact: true }).fill("2026-01-19");
  await page.getByRole("button", { name: "套用籌碼日期", exact: true }).click();
  await request;
  await expect(chart).toHaveAttribute("data-chart-ready", "true");
  expect(checks.chipRequests.every((url) => url.includes("/api/market/"))).toBeTruthy();
  expect(checks.external).toEqual([]);
  expect(checks.errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("chip-weekly.png"), fullPage: true });
});

test("mobile chip tabs are keyboard accessible and do not overflow page", async ({ page }, testInfo) => {
  const checks = await intercept(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(stockPath);
  const institution = page.getByRole("tab", { name: "三大法人", exact: true });
  await institution.focus();
  await institution.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "集保持股", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("tab", { name: "集保持股", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("img", { name: /^大戶持股比例/ })).toHaveAttribute("data-chart-ready", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.getByRole("tab", { name: "融資融券", exact: true }).click();
  await expect(page.getByRole("img", { name: /^融資餘額/ })).toHaveAttribute("data-chart-ready", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("chip-mobile.png"), fullPage: true });
  expect(checks.errors).toEqual([]);
});

for (const [mode, text] of [
  ["loading", "正在載入籌碼資料"], ["error", "籌碼 API 暫時無法連線"],
  ["empty", /尚無已收錄三大法人資料/], ["partial", "Partial · 來源欄位不完整"],
  ["stale", "Stale · 來源已標示延遲"],
] as const) {
  test(`explicit SYNTHETIC ${mode} state keeps the price chart available`, async ({ page }) => {
    await intercept(page, mode);
    await page.goto(stockPath);
    await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
    await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
    if (mode === "empty") await expect(page.getByRole("img", { name: /^法人每日買賣超/ })).toHaveCount(0);
  });
}
