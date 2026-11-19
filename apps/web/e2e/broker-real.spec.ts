/** Read-only real runtime check. No route interception, fixture or database write. */
import { expect, test } from "@playwright/test";
import { isInstrument } from "../src/lib/market-types";

test("real saved instrument keeps the existing chart and reports broker runtime availability honestly",async({page,request},testInfo)=>{
  const response=await request.get("/api/market/instruments?q=2330&market=TWSE&instrument_type=stock");
  expect(response.ok(),"Existing saved instrument API required for unmocked acceptance").toBeTruthy();
  const results=await response.json();
  const instrument:unknown=results.items.find((item:{symbol:string})=>item.symbol==="2330");
  expect(isInstrument(instrument)).toBeTruthy();
  if(!isInstrument(instrument))throw new Error("No real saved instrument");
  const errors:string[]=[];const providerRequests:string[]=[];
  page.on("pageerror",error=>errors.push(error.message));
  page.on("request",req=>{if(/https?:\/\/[^/]*(?:twse\.com\.tw|tpex\.org\.tw|tdcc\.com\.tw)/.test(req.url()))providerRequests.push(req.url());});
  await page.goto(`/stocks/${instrument.id}`);
  await expect(page.getByRole("heading",{name:new RegExp(instrument.name)})).toBeVisible();
  const brokerResponse=page.waitForResponse(result=>result.url().includes("/broker-flow?")&&result.request().method()==="GET");
  await page.getByRole("tab",{name:"主力分點",exact:true}).click();
  const saved=await brokerResponse;
  const body=await saved.json();
  await testInfo.attach("real-broker-runtime-state",{body:JSON.stringify({http_status:saved.status(),
    report_count:saved.ok()?body.count:null,code:body.detail?.code??null,
    real_broker_coverage_validated:false},null,2),contentType:"application/json"});
  if(!saved.ok())await expect(page.getByRole("alert").first()).toBeVisible();
  else if(body.count===0)await expect(page.getByText("Unavailable · 尚無已收錄券商分點資料",{exact:true})).toBeVisible();
  else test.skip(true,"Actual broker Canonical is present: separately verify source permissions and numerical coverage before claiming real-data acceptance");
  await expect(page.getByRole("table",{name:/TOP 15 買超分點/})).toHaveCount(0);
  expect(errors).toEqual([]);expect(providerRequests).toEqual([]);
});
