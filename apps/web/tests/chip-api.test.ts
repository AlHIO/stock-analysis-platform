/** SYNTHETIC offline API contracts; no real data or provider access. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../src/lib/market-api";
import { getInstitutionalHistory, getInstitutionalMetrics, getOwnershipHistory, getOwnershipMetrics, getMarginHistory, getLendingHistory } from "../src/lib/chip-api";
import { isInstitutionalHistoryResponse, isInstitutionalMetricsResponse, isOwnershipHistoryResponse, isOwnershipMetricsResponse, isMarginHistoryResponse, isLendingHistoryResponse } from "../src/lib/chip-types";
import { chipInstrumentFixture, institutionalHistoryFixture, institutionalMetricsFixture, ownershipHistoryFixture, ownershipMetricsFixture, marginHistoryFixture, lendingHistoryFixture } from "./fixtures/chip";

afterEach(()=>vi.unstubAllGlobals());
const id=chipInstrumentFixture.id;
const range={from:"2025-12-23",to:"2026-01-23"};
const clone=<T,>(value:T):T=>structuredClone(value);
const mock=(body:unknown,status=200)=>{const fetcher=vi.fn().mockResolvedValue(Response.json(body,{status}));vi.stubGlobal("fetch",fetcher);return fetcher;};

describe("canonical chip API clients",()=>{
  it.each([
    [institutionalHistoryFixture,isInstitutionalHistoryResponse],
    [institutionalMetricsFixture,isInstitutionalMetricsResponse],
    [ownershipHistoryFixture,isOwnershipHistoryResponse],
    [ownershipMetricsFixture,isOwnershipMetricsResponse],
    [marginHistoryFixture,isMarginHistoryResponse],
    [lendingHistoryFixture,isLendingHistoryResponse],
  ])("accepts the explicit synthetic backend projection",(fixture,guard)=>{
    expect(guard(fixture())).toBe(true);
  });

  it.each([
    [()=>getInstitutionalHistory(id,range),institutionalHistoryFixture,"institutional"],
    [()=>getInstitutionalMetrics(id,"foreign_ex_dealers",range),institutionalMetricsFixture,"institutional/metrics"],
    [()=>getOwnershipHistory(id,range),ownershipHistoryFixture,"ownership"],
    [()=>getOwnershipMetrics(id,400001,range),ownershipMetricsFixture,"ownership/metrics"],
    [()=>getMarginHistory(id,range),marginHistoryFixture,"margin"],
    [()=>getLendingHistory(id,"lending_short_sales",range),lendingHistoryFixture,"lending"],
  ])("uses only the same-origin allowlisted endpoint",async(call,fixture,suffix)=>{
    const fetcher=mock(fixture());
    await expect(call()).resolves.toEqual(fixture());
    const url=new URL(fetcher.mock.calls[0][0],"http://localhost");
    expect(url.pathname).toBe(`/api/market/stocks/${id}/${suffix}`);
    expect(url.searchParams.get("from")).toBe(range.from);
    expect(url.searchParams.get("to")).toBe(range.to);
    expect(fetcher.mock.calls[0][1]).toMatchObject({cache:"no-store",signal:expect.any(AbortSignal)});
    if(suffix!=="institutional/metrics") expect(url.searchParams.get("limit")).toBe("1000");
    else expect(url.searchParams.has("period")).toBe(false);
  });

  it("preserves exact quantities above the JavaScript safe integer range",async()=>{
    const response=marginHistoryFixture();
    response.items[0].financing.balance.value="9223372036854775807";
    mock(response);
    const data=await getMarginHistory(id,range);
    expect(data.items[0].financing.balance.value).toBe("9223372036854775807");
  });

  it("leaves cursor and partial coverage visible instead of claiming a full range",async()=>{
    const response={...ownershipHistoryFixture(),next_cursor:"SYNTHETIC-next-page",coverage:{...ownershipHistoryFixture().coverage,truncated:true}};
    mock(response);const result=await getOwnershipHistory(id,range);
    expect(result.next_cursor).toBe("SYNTHETIC-next-page");expect(result.coverage.truncated).toBe(true);
  });

  it("requests borrowing outstanding as a separate report kind",async()=>{
    const body=lendingHistoryFixture("borrowed_positions");const fetcher=mock(body);
    await expect(getLendingHistory(id,"borrowed_positions",range)).resolves.toEqual(body);
    expect(new URL(fetcher.mock.calls[0][0],"http://localhost").searchParams.get("report_kind")).toBe("borrowed_positions");
  });

  it.each(["instrument","range","category","threshold","kind"])("rejects a response belonging to another %s",async(field)=>{
    let body:unknown;let call:()=>Promise<unknown>;
    if(field==="category") {body={...institutionalMetricsFixture(),category:"dealer_total"};call=()=>getInstitutionalMetrics(id,"foreign_ex_dealers",range);}
    else if(field==="threshold") {body={...ownershipMetricsFixture(),threshold:"1000001"};call=()=>getOwnershipMetrics(id,400001,range);}
    else if(field==="kind") {body=lendingHistoryFixture("borrowed_positions");call=()=>getLendingHistory(id,"lending_short_sales",range);}
    else {body={...marginHistoryFixture(),...(field==="range"?{from:"2025-12-24"}:{instrument:{...chipInstrumentFixture,id:"00000000-0000-0000-0000-000000000099"}})};call=()=>getMarginHistory(id,range);}
    mock(body);await expect(call()).rejects.toMatchObject({status:502,code:"invalid_response"});
  });

  it.each([404,422,503])("retains safe public API status %s",async(status)=>{
    mock({detail:{code:"ambiguous_lending_channel",message:"private SQL should not display"}},status);
    await expect(getLendingHistory(id,"lending_short_sales",range)).rejects.toMatchObject({status,code:"ambiguous_lending_channel"});
  });
  it("returns API unavailable on timeout and preserves explicit cancellation",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockRejectedValue(new DOMException("timeout","TimeoutError")));
    await expect(getMarginHistory(id,range)).rejects.toMatchObject({status:503});
    const controller=new AbortController();controller.abort();const aborted=new DOMException("cancelled","AbortError");
    vi.stubGlobal("fetch",vi.fn().mockRejectedValue(aborted));
    await expect(getMarginHistory(id,range,controller.signal)).rejects.toBe(aborted);
  });

  it("rejects unsafe threshold input before fetching",async()=>{
    const fetcher=vi.fn();vi.stubGlobal("fetch",fetcher);
    await expect(getOwnershipMetrics(id,Number.MAX_SAFE_INTEGER+1,range)).rejects.toBeInstanceOf(ApiError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("financial shape and date guards",()=>{
  it("rejects numeric quantities, negative balances, mixed units and out-of-range BIGINT",()=>{
    for(const value of [123,-1,"-1","9223372036854775808"]){
      const data=clone(marginHistoryFixture());
      (data.items[0].financing.balance as {value:unknown}).value=value;
      expect(isMarginHistoryResponse(data)).toBe(false);
    }
    const mixed=clone(marginHistoryFixture());mixed.items[0].financing.balance.unit="units";
    expect(isMarginHistoryResponse(mixed)).toBe(false);
    const mixedMetric=clone(marginHistoryFixture());mixedMetric.items[0].financing.daily_change.quantity_unit="units";
    expect(isMarginHistoryResponse(mixedMetric)).toBe(false);
  });
  it("preserves missing fields and genuine zero without filling gaps",()=>{
    const data=clone(lendingHistoryFixture());
    expect(data.items[0].ledger.signed_adjustment.value).toBe("0");
    data.items[0].ledger.balance={value:null,availability:"missing",unit:"shares"};
    expect(isLendingHistoryResponse(data)).toBe(true);
    data.items[0].ledger.balance.availability="reported";
    expect(isLendingHistoryResponse(data)).toBe(false);
  });
  it("rejects duplicate dates, count mismatch and dates outside the query",()=>{
    const data=ownershipHistoryFixture();
    expect(isOwnershipHistoryResponse({...data,count:data.count+1})).toBe(false);
    expect(isOwnershipHistoryResponse({...data,items:[data.items[0],...data.items],count:data.count+1})).toBe(false);
    expect(isOwnershipHistoryResponse({...data,from:"2026-01-10"})).toBe(false);
  });
  it("rejects malformed weekly buckets and regular negative holdings",()=>{
    const data=clone(ownershipHistoryFixture());data.items[0].buckets[0].quantity="-1";
    expect(isOwnershipHistoryResponse(data)).toBe(false);
    const duplicated=ownershipHistoryFixture();duplicated.items[0].buckets.push(duplicated.items[0].buckets[0]);
    expect(isOwnershipHistoryResponse(duplicated)).toBe(false);
  });
  it("requires distinct 5/10/20/60 institutional windows and correct units",()=>{
    const data=institutionalMetricsFixture();data.cumulative[1].period=5;
    expect(isInstitutionalMetricsResponse(data)).toBe(false);
    const history=clone(institutionalHistoryFixture());history.items[0].components[0].buy_quantity="-1";
    expect(isInstitutionalHistoryResponse(history)).toBe(false);
    const mixed=institutionalHistoryFixture();mixed.items[0].derived_daily[0].quantity_unit="units";
    expect(isInstitutionalHistoryResponse(mixed)).toBe(false);
  });
  it("rejects mixed observation units even when each observation is internally consistent",()=>{
    const institution=clone(institutionalHistoryFixture());
    institution.items[0].quantity_unit="units";
    institution.items[0].derived_daily.forEach(value=>{value.quantity_unit="units";});
    expect(isInstitutionalHistoryResponse(institution)).toBe(false);
    const ownership=clone(ownershipHistoryFixture());ownership.items[0].quantity_unit="units";
    expect(isOwnershipHistoryResponse(ownership)).toBe(false);
    const margin=clone(marginHistoryFixture());const item=margin.items[0];item.quantity_unit="units";
    for(const ledger of [item.financing,item.short_margin]){
      for(const quantity of [ledger.balance,ledger.previous_balance,ledger.signed_adjustment])quantity.unit="units";
      for(const metric of [ledger.identity,ledger.daily_change,ledger.available_quota])metric.quantity_unit="units";
    }
    expect(isMarginHistoryResponse(margin)).toBe(false);
    const lending=clone(lendingHistoryFixture());const point=lending.items[0];point.quantity_unit="units";
    const ledger=point.ledger;
    const flows="short_sales" in ledger?[ledger.short_sales,ledger.short_returns]:[ledger.borrowed_quantity,ledger.returned_quantity];
    for(const quantity of [ledger.balance,ledger.previous_balance,ledger.signed_adjustment,...flows])quantity.unit="units";
    point.ledger.identity.quantity_unit="units";
    expect(isLendingHistoryResponse(lending)).toBe(false);
  });
  it("rejects point scopes that differ from the requested margin or lending scope",()=>{
    const margin=clone(marginHistoryFixture());margin.items[0].balance_scope="SYNTHETIC-other-scope";
    expect(isMarginHistoryResponse(margin)).toBe(false);
    const lending=clone(lendingHistoryFixture());lending.items[0].trade_scope="SYNTHETIC-other-scope";
    expect(isLendingHistoryResponse(lending)).toBe(false);
  });
  it.each(["source","contract","taxonomy","scope_code","scope_basis","scope_flag"])("does not connect institutional observations with an incompatible %s profile",field=>{
    const body=clone(institutionalHistoryFixture());const point=body.items[0];
    if(field==="source")point.provenance={...point.provenance,source:{...point.provenance.source,id:"00000000-0000-0000-0000-000000000099"}};
    if(field==="contract")point.provenance={...point.provenance,source_contract_version:"SYNTHETIC-other-contract"};
    if(field==="taxonomy")point.taxonomy_version="SYNTHETIC-other-taxonomy";
    if(field==="scope_code")point.trade_scope={...point.trade_scope,code:"SYNTHETIC-other-scope"};
    if(field==="scope_basis")point.trade_scope={...point.trade_scope,basis:"corrected"};
    if(field==="scope_flag")point.trade_scope={...point.trade_scope,includes_block_trades:!point.trade_scope.includes_block_trades};
    expect(isInstitutionalHistoryResponse(body)).toBe(false);
  });
  it("accepts different institutional input revisions within the same compatible profile",()=>{
    const body=clone(institutionalHistoryFixture());
    body.items[0].provenance={...body.items[0].provenance,revision_id:"00000000-0000-0000-0000-000000000099",raw_id:"00000000-0000-0000-0000-000000000098",run_id:"00000000-0000-0000-0000-000000000097"};
    expect(isInstitutionalHistoryResponse(body)).toBe(true);
  });
  it("keeps independently selectable lending channels valid without combining them",()=>{
    const body=clone(lendingHistoryFixture());const extra=clone(body.items.at(-1)!);
    extra.lending_channel={...extra.lending_channel,family:"ZZ-SYNTHETIC-other-family",code:"ZZ-SYNTHETIC-other-channel"};
    body.items.push(extra);body.count=body.items.length;
    expect(isLendingHistoryResponse(body)).toBe(true);
  });
  it("reports a safe invalid response instead of drawing an incompatible source history",async()=>{
    const body=clone(institutionalHistoryFixture());
    body.items[0].provenance={...body.items[0].provenance,source_contract_version:"SYNTHETIC-unreviewed-contract"};
    mock(body);
    await expect(getInstitutionalHistory(id,range)).rejects.toMatchObject({status:502,code:"invalid_response"});
  });
  it("accepts signed source adjustments but rejects coercible status arrays",()=>{
    const valid=lendingHistoryFixture();valid.items[0].ledger.signed_adjustment.value="-7";
    expect(isLendingHistoryResponse(valid)).toBe(true);
    const invalid=marginHistoryFixture();
    (invalid.items[0].quality as {status:unknown}).status=["verified"];
    expect(isMarginHistoryResponse(invalid)).toBe(false);
  });
  it("does not confuse borrowing quantities with lending short sale fields",()=>{
    const data=lendingHistoryFixture("borrowed_positions");
    expect(isLendingHistoryResponse(data)).toBe(true);
    (data.items[0].ledger as Record<string,unknown>).short_sales={value:"10",unit:"shares",availability:"reported"};
    expect(isLendingHistoryResponse(data)).toBe(false);
  });
});
