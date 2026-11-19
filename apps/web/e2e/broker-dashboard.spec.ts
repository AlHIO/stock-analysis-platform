/** SYNTHETIC UI-only acceptance; not official broker coverage or a production seed. */
import { expect, test, type Page } from "@playwright/test";
import { chipBarsFixture, chipLatestFixture, institutionalHistoryFixture, institutionalMetricsFixture,
  ownershipMetricsFixture } from "../tests/fixtures/chip";
import { brokerHistoryFixture, brokerLatestFixture, brokerMetricFixture, brokerIds } from "../tests/fixtures/broker";
import type { BrokerAlgorithm, BrokerMetricResult } from "../src/lib/broker-types";

const id=chipLatestFixture.instrument.id;
const other="00000000-0000-0000-0000-000000000005";
const at="2026-01-23T12:00:00Z";
type Mode="ready"|"error"|"empty"|"partial"|"loading";

async function intercept(page:Page,mode:Mode="ready") {
  const errors:string[]=[]; const external:string[]=[];
  page.on("pageerror",error=>errors.push(error.message));
  page.on("request",request=>{if(/https?:\/\/[^/]*(?:twse\.com\.tw|tpex\.org\.tw|tdcc\.com\.tw)/.test(request.url()))external.push(request.url());});
  await page.route("**/api/market/**",async route=>{
    const url=new URL(route.request().url());const path=url.pathname;
    const range={from:url.searchParams.get("from")??"2025-10-25",to:url.searchParams.get("to")??"2026-01-23"};
    if(!path.includes("broker-flow")) {
      if(path.endsWith("/latest"))return route.fulfill({json:chipLatestFixture});
      if(path.endsWith("/bars"))return route.fulfill({json:{...chipBarsFixture,...range,timeframe:url.searchParams.get("timeframe")}});
      if(path.endsWith("/institutional/metrics"))return route.fulfill({json:{...institutionalMetricsFixture(url.searchParams.get("category")??"foreign_ex_dealers"),...range}});
      if(path.endsWith("/institutional"))return route.fulfill({json:{...institutionalHistoryFixture(),...range}});
      if(path.endsWith("/ownership/metrics")){
        const data=ownershipMetricsFixture();
        const items=data.items.filter(item=>range.from<=item.as_of_date&&item.as_of_date<=range.to);
        return route.fulfill({json:{...data,...range,items,count:items.length,threshold:url.searchParams.get("threshold")??"400001"}});
      }
      return route.fulfill({status:404,json:{detail:{code:"not_found",message:"SYNTHETIC absent",errors:null}}});
    }
    if(mode==="loading")await new Promise<void>(resolve=>setTimeout(resolve,1400));
    if(mode==="error")return route.fulfill({status:503,json:{detail:{code:"database_unavailable",message:"SYNTHETIC outage",errors:null}}});
    const history=brokerHistoryFixture();history.instrument=structuredClone(chipLatestFixture.instrument);
    const report=history.items[0];
    const identity={...report.trades[0].identity,branch_id:other,identity_id:other,branch_name:"SYNTHETIC 賣超分點"};
    report.trades.push({...report.trades[0],branch_id:other,identity_id:other,identity,buy_quantity:"0",sell_quantity:"300",net_quantity:"-300",reported_net_quantity:"-300",buy_amount:"0",sell_amount:"6000",net_amount:"-6000"});
    report.total_branch_count=2;
    if(path.endsWith("/broker-flow"))return route.fulfill({json:{...history,...range,
      items:mode==="empty"?[]:history.items,count:mode==="empty"?0:history.count,limit:Number(url.searchParams.get("limit")??50)}});
    if(path.endsWith("/latest"))return route.fulfill({json:{...brokerLatestFixture(),instrument:history.instrument,point:mode==="empty"?null:report}});
    const isTop=path.endsWith("/top-branches");
    const algorithm=(isTop?"broker.top_branches":url.searchParams.get("algorithm")??"broker.source_coverage") as BrokerAlgorithm;
    const parameters=isTop?{top_n:Number(url.searchParams.get("top_n")??15),side:url.searchParams.get("side")??"net_buyers"}:JSON.parse(url.searchParams.get("algorithm_parameters")??"{}");
    const sell=parameters.side==="net_sellers";
    const base=brokerMetricFixture({metric:algorithm,parameters});
    base.instrument=history.instrument; base.read_at=at;
    base.result.request={...base.result.request,start:range.from,end:range.to,query_as_of:at};
    base.result.input_refs[0].metadata.identities=[report.trades[0].identity,identity];
    if(isTop)base.result.value=[{branch_id:sell?other:brokerIds.branch,buy_quantity:sell?"0":"1000",sell_quantity:sell?"300":"100",net_quantity:sell?"-300":"900",rank:1,identity_ids:[sell?other:brokerIds.identity]}];
    else if(algorithm==="proxy.broker_top_n_net_imbalance") {
      base.result.value="600";base.result.details={selected_buy_quantity:"1000",selected_sell_quantity:"400",selection_basis:"union_of_top_net_buyers_and_sellers"};
    } else if(algorithm==="proxy.broker_daily_top_n_imbalance_sum") {
      base.result.value="600";base.result.details={daily:[{trade_date:range.to,proxy_net:"600"}]};
    } else if(algorithm.endsWith("count_difference")) {
      base.result.value=[{trade_date:range.to,value:algorithm.includes("active_")?-1:0}];base.result.unit="branches";
    } else base.result.value="900";
    const sessions=url.searchParams.get("sessions");
    if(sessions)base.result.details.session_window={requested_sessions:Number(sessions),verified:false,query_from:range.from,query_to:range.to,expected_dates:[]};
    if(mode==="empty"||mode==="partial"||sessions){base.result.status=mode==="partial"?"partial":"not_verified";base.result.value=null;}
    return route.fulfill({json:base});
  });
  return {errors,external};
}

test("desktop broker ranks, verified-window limitation, branch choice and persistent Tab settings",async({page},testInfo)=>{
  const checks=await intercept(page);
  await page.goto(`/stocks/${id}`);
  await page.getByRole("tab",{name:"主力分點",exact:true}).click();
  await expect(page.getByText("偏買超（Proxy）",{exact:true})).toBeVisible();
  const buyers=page.getByRole("table",{name:/TOP 15 買超分點/});
  await expect(buyers.getByRole("button",{name:"SYNTHETIC 測試分點",exact:true})).toBeVisible();
  await expect(buyers.getByText("900",{exact:true})).toBeVisible();
  await expect(page.getByRole("table",{name:/TOP 15 賣超分點/}).getByText("-300",{exact:true})).toBeVisible();
  await expect(page.getByText(/不假設一張 1,000 股/)).toBeVisible();
  await page.getByRole("tabpanel",{name:"主力分點",exact:true}).screenshot({path:testInfo.outputPath("broker-desktop-SYNTHETIC.png")});
  await page.getByRole("combobox",{name:"指定分點",exact:true}).selectOption(brokerIds.branch);
  await expect(page.getByText("指定分點 1 交易日累計買賣超",{exact:true}).locator("..")).toContainText("900");
  await page.getByRole("button",{name:"5 個交易日",exact:true}).click();
  await expect(page.getByText(/未取得核實完整區間/).first()).toBeVisible();
  await expect(page.getByText("偏買超（Proxy）",{exact:true})).toHaveCount(0);
  await page.getByRole("tab",{name:"集保持股",exact:true}).click();
  await page.getByRole("tab",{name:"主力分點",exact:true}).click();
  await expect(page.getByRole("button",{name:"5 個交易日",exact:true})).toHaveAttribute("aria-pressed","true");
  expect(checks.errors).toEqual([]);expect(checks.external).toEqual([]);
});

test("weekly K-line subpane keeps source dates and exact crosshair gaps on desktop",async({page})=>{
  const checks=await intercept(page);
  await page.goto(`/stocks/${id}`);
  await page.getByRole("checkbox",{name:"集保大戶比例（週）",exact:true}).check();
  const weekly=page.locator('[data-testid*="subpane-ownership"]');
  await expect(weekly.getByRole("img",{name:"集保大戶持股比例，每週統計，單位%",exact:true})).toHaveAttribute("data-chart-ready","true");
  await expect(weekly).toContainText("每週統計");
  const main=page.getByLabel("日 K與成交量圖",{exact:true});
  await expect(main.locator("canvas").first()).toBeVisible();
  const bounds=await main.locator("canvas").first().boundingBox();
  if(!bounds)throw new Error("SYNTHETIC main canvas unavailable");
  let sawGap=false;
  for(let x=40;x<bounds.width-80;x+=18){
    await page.mouse.move(bounds.x+x,bounds.y+bounds.height*.45);
    if((await weekly.textContent())?.includes("此日無觀察")){sawGap=true;break;}
  }
  expect(sawGap,"daily cursor must not turn a missing weekly date into a daily observation").toBeTruthy();
  await page.getByRole("checkbox",{name:"顯示成交量副圖",exact:true}).uncheck();
  await expect(page.getByLabel("日 K價格圖",{exact:true})).toBeVisible();
  expect(checks.errors).toEqual([]);
});

for(const mode of ["error","empty","partial","loading"] as const){
  test(`broker ${mode} has an explicit state and no invented complete ranks`,async({page})=>{
    await intercept(page,mode);await page.goto(`/stocks/${id}`);
    await page.getByRole("tab",{name:"主力分點",exact:true}).click();
    if(mode==="error")await expect(page.getByRole("alert").filter({hasText:"分點 API 暫時無法連線"}).first()).toBeVisible();
    else if(mode==="empty")await expect(page.getByText("Unavailable · 尚無已收錄券商分點資料",{exact:true})).toBeVisible();
    else if(mode==="partial")await expect(page.getByText(/Partial/).first()).toBeVisible();
    else await expect(page.getByText("正在載入分點資料…",{exact:true})).toBeVisible();
    if(mode!=="loading")await expect(page.getByRole("table",{name:/TOP 15 買超分點/})).toHaveCount(0);
  });
}

test("mobile has responsive broker controls and horizontally contained ranking tables",async({page},testInfo)=>{
  await page.setViewportSize({width:390,height:844});const checks=await intercept(page);
  await page.goto(`/stocks/${id}`);await page.getByRole("tab",{name:"主力分點",exact:true}).click();
  await expect(page.getByRole("table",{name:/TOP 15 買超分點/})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
  await page.getByRole("tabpanel",{name:"主力分點",exact:true}).screenshot({path:testInfo.outputPath("broker-mobile-SYNTHETIC.png")});
  await page.getByRole("button",{name:"60 個交易日",exact:true}).click();
  await expect(page.getByRole("button",{name:"60 個交易日",exact:true})).toHaveAttribute("aria-pressed","true");
  expect(checks.errors).toEqual([]);expect(checks.external).toEqual([]);
});
