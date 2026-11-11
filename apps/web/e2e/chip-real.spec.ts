/**
 * Real Canonical acceptance: no route interception, fixtures or database writes.
 * Missing source coverage is skipped with an explicit reason, never synthesized.
 * Browser UI uses the same-origin proxy; direct backend reads below are confined
 * to Playwright's Node APIRequestContext to verify cross-source lineage.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { isInstrument, type Instrument } from "../src/lib/market-types";
import { isOwnershipHistoryResponse, isOwnershipMetricsResponse,
  isMarginHistoryResponse, isLendingHistoryResponse } from "../src/lib/chip-types";
import { decimal } from "../src/lib/format";

async function savedInstrument(request: APIRequestContext, symbol: string, market: string): Promise<Instrument> {
  const response = await request.get(`/api/market/instruments?q=${symbol}&market=${market}&instrument_type=stock`);
  expect(response.ok(), "Start the local frontend and backend before real-data acceptance").toBeTruthy();
  const data = await response.json();
  const found: unknown = data.items.find((item: {symbol:string}) => item.symbol === symbol);
  expect(isInstrument(found), `Real saved ${symbol} instrument is required`).toBeTruthy();
  if (!isInstrument(found)) throw new Error("Saved instrument unavailable");
  return found;
}

async function savedRange(request: APIRequestContext, id: string) {
  const response = await request.get(`/api/market/stocks/${id}/latest`);
  expect(response.ok()).toBeTruthy();
  const latest = await response.json();
  const to: string = latest.freshness.market_local_date;
  const start = new Date(`${to}T00:00:00Z`); start.setUTCDate(start.getUTCDate() - 365);
  return {from:start.toISOString().slice(0,10),to};
}

function observeBrowser(page: Page) {
  const errors: string[] = [], unexpected: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/") && (request.method() !== "GET" ||
      !url.pathname.startsWith("/api/market/"))) unexpected.push(request.url());
    if (/(?:twse\.com\.tw|tpex\.org\.tw|tdcc\.com\.tw)$/.test(url.hostname)) unexpected.push(request.url());
  });
  return {errors,unexpected};
}

test("real TDCC Canonical weekly distribution matches the UI and analytical input Revision", async ({page,request}, testInfo) => {
  const instrument = await savedInstrument(request,"2330","TWSE");
  const range = await savedRange(request,instrument.id);
  const query = new URLSearchParams({...range,limit:"1000",order:"asc"});
  const historyResponse = await request.get(`/api/market/stocks/${instrument.id}/ownership?${query}`);
  expect(historyResponse.ok()).toBeTruthy();
  const history: unknown = await historyResponse.json();
  expect(isOwnershipHistoryResponse(history)).toBeTruthy();
  if (!isOwnershipHistoryResponse(history)) throw new Error("Ownership Canonical contract mismatch");
  test.skip(history.items.length === 0,"Blocked real TDCC acceptance: no saved weekly Canonical in the bounded range");
  const report = history.items.at(-1)!;
  const metricsResponse = await request.get(`/api/market/stocks/${instrument.id}/ownership/metrics?${query}&threshold=400001&threshold_unit=shares`);
  expect(metricsResponse.ok()).toBeTruthy();
  const metrics: unknown = await metricsResponse.json();
  expect(isOwnershipMetricsResponse(metrics)).toBeTruthy();
  if (!isOwnershipMetricsResponse(metrics)) throw new Error("Ownership metrics contract mismatch");
  const metric = metrics.items.find(point=>point.as_of_date === report.as_of_date)!;
  expect(metric).toBeTruthy();
  const checks = observeBrowser(page);
  await page.goto(`/stocks/${instrument.id}`);
  await page.getByRole("tab",{name:"集保持股",exact:true}).click();
  await page.getByRole("button",{name:"近1年",exact:true}).click();
  if (metric.ratio.status === "complete") {
    await expect(page.getByText(`${decimal(metric.ratio.value)} %`,{exact:true})).toBeVisible();
    await expect(page.getByRole("img",{name:/^大戶持股比例/})).toHaveAttribute("data-chart-ready","true");
  }
  await expect(page.getByRole("combobox",{name:/級距分布統計日/})).toHaveValue(report.as_of_date);
  const table = page.getByRole("table",{name:/持股級距分布/});
  await expect(table.getByRole("row")).toHaveCount(report.buckets.length+1);
  for (const [index,bucket] of report.buckets.entries()) {
    const row = table.getByRole("row").nth(index+1);
    await expect(row.getByRole("rowheader")).toContainText(bucket.label);
    await expect(row.getByRole("cell").nth(0)).toHaveText(decimal(bucket.holder_count));
    await expect(row.getByRole("cell").nth(1)).toHaveText(decimal(bucket.quantity));
    await expect(row.getByRole("cell").nth(2)).toHaveText(`${decimal(bucket.reported_percent)}${bucket.reported_percent===null?"":"%"}`);
  }
  const selectedSource=page.getByText("所選週報來源與版本",{exact:true}).locator("..");
  await selectedSource.locator("summary").click();
  await expect(selectedSource.getByText(report.provenance.revision_id,{exact:true})).toBeVisible();
  if (report.timing.source_published_at===null && report.timing.source_published_on===null)
    await expect(selectedSource.getByText("未知，未自行推算",{exact:true})).toBeVisible();
  // Cross-source API is tested by the harness, not fetched by product UI.
  const backend = process.env.BROWSER_BACKEND_URL ?? "http://127.0.0.1:8000";
  const cross = await request.get(`${backend}/api/v1/stocks/${instrument.id}/chip-metrics?metric=ownership_large_holder_percent&from=${report.as_of_date}&to=${report.as_of_date}&time_basis=query_as_of`);
  expect(cross.ok()).toBeTruthy();
  const analytical = await cross.json();
  expect(analytical.coverage.point_in_time_supported).toBe(false);
  const point = analytical.points.find((p:{metric:string})=>p.metric==="ownership_large_holder_percent");
  expect(point.input_refs.some((ref:{revision_id:string})=>ref.revision_id===report.provenance.revision_id)).toBe(true);
  if (metric.ratio.status==="complete") expect(point.value).toBe(metric.ratio.value);
  expect(checks.errors).toEqual([]); expect(checks.unexpected).toEqual([]);
  await page.screenshot({path:testInfo.outputPath("real-ownership.png"),fullPage:true});
});

test("real margin and lending Canonical values remain distinct on mobile, with unavailable sources disclosed", async ({page,request},testInfo) => {
  const instrument = await savedInstrument(request,"6488","TPEX");
  const range = await savedRange(request,instrument.id);
  const query = new URLSearchParams({...range,limit:"1000",order:"asc"});
  const marginResponse = await request.get(`/api/market/stocks/${instrument.id}/margin?${query}`);
  expect(marginResponse.ok()).toBeTruthy();
  const margin: unknown = await marginResponse.json();
  expect(isMarginHistoryResponse(margin)).toBeTruthy();
  if (!isMarginHistoryResponse(margin)) throw new Error("Margin Canonical contract mismatch");
  const lendingResponse = await request.get(`/api/market/stocks/${instrument.id}/lending?${query}&report_kind=lending_short_sales`);
  expect(lendingResponse.ok()).toBeTruthy();
  const lending: unknown = await lendingResponse.json();
  expect(isLendingHistoryResponse(lending)).toBeTruthy();
  if (!isLendingHistoryResponse(lending)) throw new Error("Lending Canonical contract mismatch");
  test.skip(!margin.items.length || !lending.items.length,"Blocked real margin/lending acceptance: saved Canonical coverage is missing");
  const marginReport=margin.items.at(-1)!, lendingReport=lending.items.at(-1)!;
  await page.setViewportSize({width:390,height:844});
  const checks=observeBrowser(page);
  await page.goto(`/stocks/${instrument.id}`);
  await page.getByRole("button",{name:"近1年",exact:true}).click();
  await page.getByRole("tab",{name:"融資融券",exact:true}).click();
  await expect(page.getByText(`${decimal(marginReport.financing.balance.value)}`,{exact:true}).first()).toBeVisible();
  await expect(page.getByText(`${decimal(marginReport.short_margin.balance.value)}`,{exact:true}).first()).toBeVisible();
  await expect(page.getByRole("img",{name:/^融資餘額/})).toHaveAttribute("data-chart-ready","true");
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:testInfo.outputPath("real-margin-mobile.png"),fullPage:true});
  await page.getByRole("tab",{name:"借券賣出",exact:true}).click();
  await expect(page.getByText(decimal(lendingReport.ledger.balance.value),{exact:true}).first()).toBeVisible();
  if (!lendingReport.lending_channel.verified || lendingReport.trade_scope==="unspecified")
    await expect(page.getByText(/來源範圍未核實/).first()).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("real-lending-mobile.png"),fullPage:true});
  const backend=process.env.BROWSER_BACKEND_URL ?? "http://127.0.0.1:8000";
  const from=marginReport.trade_date<lendingReport.trade_date?marginReport.trade_date:lendingReport.trade_date;
  const to=marginReport.trade_date>lendingReport.trade_date?marginReport.trade_date:lendingReport.trade_date;
  const cross=await request.get(`${backend}/api/v1/stocks/${instrument.id}/chip-metrics?metric=margin_balance_change&metric=lending_balance&metric=institutional_net_sum&from=${from}&to=${to}&time_basis=query_as_of`);
  expect(cross.ok()).toBeTruthy();
  const analytical=await cross.json();
  const marginPoint=analytical.points.find((p:{metric:string;evaluation_date:string})=>p.metric==="margin_balance_change"&&p.evaluation_date===marginReport.trade_date);
  expect(decimal(marginPoint.value)).toBe(decimal(marginReport.financing.daily_change.value));
  expect(marginPoint.input_refs.some((ref:{revision_id:string})=>ref.revision_id===marginReport.provenance.revision_id)).toBe(true);
  const lendingPoint=analytical.points.find((p:{metric:string;evaluation_date:string})=>p.metric==="lending_balance"&&p.evaluation_date===lendingReport.trade_date);
  expect(lendingPoint.input_refs.some((ref:{revision_id:string})=>ref.revision_id===lendingReport.provenance.revision_id)).toBe(true);
  if (!lendingReport.lending_channel.verified || lendingReport.trade_scope==="unspecified") {
    expect(lendingPoint.value).toBeNull(); expect(lendingPoint.status).toBe("not_verified");
  }
  await page.getByRole("combobox",{name:/借券報表類型/}).selectOption("borrowed_positions");
  const borrowed=await request.get(`/api/market/stocks/${instrument.id}/lending?${query}&report_kind=borrowed_positions`);
  expect(borrowed.ok()).toBeTruthy();
  const borrowedData=await borrowed.json();
  if (borrowedData.items.length===0) await expect(page.getByRole("heading",{name:"尚無可用的借券未還報表"})).toBeVisible();
  const institutional=await request.get(`/api/market/stocks/${instrument.id}/institutional?${query}`);
  expect(institutional.ok()).toBeTruthy();
  const institutionalData=await institutional.json();
  await page.getByRole("tab",{name:"三大法人",exact:true}).click();
  if (institutionalData.items.length===0) {
    await expect(page.getByRole("heading",{name:"尚無已收錄三大法人資料"})).toBeVisible();
    expect(analytical.points.filter((p:{metric:string})=>p.metric==="institutional_net_sum").every((p:{value:null})=>p.value===null)).toBe(true);
  }
  expect(checks.errors).toEqual([]); expect(checks.unexpected).toEqual([]);
  await page.screenshot({path:testInfo.outputPath("real-unavailable-institutional-mobile.png"),fullPage:true});
});
