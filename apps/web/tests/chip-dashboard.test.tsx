import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChipDashboard } from "../src/components/chip-dashboard";
import * as api from "../src/lib/chip-api";
import * as types from "../src/lib/chip-types";
import { ApiError, getBars } from "../src/lib/market-api";
import { isBarsResponse } from "../src/lib/market-types";
import { chipBarsFixture, chipInstrumentFixture, institutionalHistoryFixture,
  institutionalMetricsFixture, ownershipHistoryFixture, ownershipMetricsFixture,
  marginHistoryFixture, lendingHistoryFixture } from "./fixtures/chip";
import type { ChipSeries } from "../src/lib/chip-chart";

vi.mock("../src/lib/chip-api", async(original)=>({...await original<typeof api>(),
  getInstitutionalHistory:vi.fn(),getInstitutionalMetrics:vi.fn(),getOwnershipHistory:vi.fn(),
  getOwnershipMetrics:vi.fn(),getMarginHistory:vi.fn(),getLendingHistory:vi.fn()}));
vi.mock("../src/lib/market-api",async(original)=>({...await original<typeof import("../src/lib/market-api")>(),getBars:vi.fn()}));
vi.mock("../src/components/chip-chart",()=>({ChipChart:({title,series,frequency}:{title:string;series:ChipSeries[];frequency:string})=><div data-testid={title} data-frequency={frequency}>{JSON.stringify(series)}</div>}));

function checked<T>(data:unknown, guard:(v:unknown)=>v is T):T {
  if(!guard(data))throw Error("SYNTHETIC fixture rejected");return data;
}
function boot(){render(<ChipDashboard id={chipInstrumentFixture.id} marketDate="2026-01-23" currency="TWD"/>);}
function panel(){return screen.getByRole("tabpanel");}
beforeEach(()=>{
  vi.clearAllMocks();
  vi.mocked(api.getInstitutionalHistory).mockImplementation(async(_id,range)=>({...checked(institutionalHistoryFixture(),types.isInstitutionalHistoryResponse),...range}));
  vi.mocked(api.getInstitutionalMetrics).mockImplementation(async(_id,category,range)=>({...checked(institutionalMetricsFixture(category),types.isInstitutionalMetricsResponse),...range}));
  vi.mocked(api.getOwnershipHistory).mockImplementation(async(_id,range)=>({...checked(ownershipHistoryFixture(),types.isOwnershipHistoryResponse),...range}));
  vi.mocked(api.getOwnershipMetrics).mockImplementation(async(_id,threshold,range)=>({...checked(ownershipMetricsFixture(),types.isOwnershipMetricsResponse),threshold:String(threshold),...range}));
  vi.mocked(api.getMarginHistory).mockImplementation(async(_id,range)=>({...checked(marginHistoryFixture(),types.isMarginHistoryResponse),...range}));
  vi.mocked(api.getLendingHistory).mockImplementation(async(_id,kind,range)=>({...checked(lendingHistoryFixture(kind),types.isLendingHistoryResponse),...range}));
  vi.mocked(getBars).mockResolvedValue(checked(chipBarsFixture,isBarsResponse));
});
afterEach(cleanup);

describe("SYNTHETIC ChipDashboard",()=>{
  it("loads backend institutional windows without recalculating and visits panels lazily",async()=>{
    boot();await screen.findByTestId("法人每日買賣超");
    expect(api.getOwnershipHistory).not.toHaveBeenCalled();
    expect(api.getMarginHistory).not.toHaveBeenCalled();
    expect(within(panel()).getByText("5 交易日累計")).toBeVisible();
    expect(within(panel()).getByText(/連買 3 個交易日/)).toBeVisible();
    expect(screen.getByTestId("區間收盤價")).toBeVisible();
  });

  it("keeps visited charts and selected thresholds mounted across tabs",async()=>{
    boot();await screen.findByTestId("法人每日買賣超");
    fireEvent.click(screen.getByRole("tab",{name:"集保持股"}));
    await screen.findByTestId("大戶持股比例");
    fireEvent.change(screen.getByLabelText("大戶門檻"),{target:{value:"1000001"}});
    await waitFor(()=>expect(api.getOwnershipMetrics).toHaveBeenLastCalledWith(chipInstrumentFixture.id,1000001,expect.anything(),expect.any(AbortSignal)));
    const ratio=screen.getByTestId("大戶持股比例");
    fireEvent.click(screen.getByRole("tab",{name:"融資融券"}));
    await screen.findByTestId("融資餘額");
    fireEvent.click(screen.getByRole("tab",{name:"集保持股"}));
    expect(screen.getByLabelText("大戶門檻")).toHaveValue("1000001");
    expect(screen.getByTestId("大戶持股比例")).toBe(ratio);
    expect(api.getOwnershipHistory).toHaveBeenCalledTimes(1);
  });

  it("does not relabel the previous category metrics while a new category is loading",async()=>{
    boot();await screen.findByText(/連買 3 個交易日/);
    vi.mocked(api.getInstitutionalMetrics).mockImplementationOnce(()=>new Promise(()=>{}));
    fireEvent.change(screen.getByLabelText("法人分類"),{target:{value:"investment_trust"}});
    expect(screen.queryByText(/連買 3 個交易日/)).not.toBeInTheDocument();
    expect(screen.getByText(/連買 — 個交易日/)).toBeVisible();
  });

  it("does not display the previous threshold ratio under a new ownership threshold",async()=>{
    boot();fireEvent.click(screen.getByRole("tab",{name:"集保持股"}));
    await waitFor(()=>expect(screen.getByTestId("大戶持股比例").textContent).toContain('"value":"70.000000000000"'));
    vi.mocked(api.getOwnershipMetrics).mockImplementationOnce(()=>new Promise(()=>{}));
    fireEvent.change(screen.getByLabelText("大戶門檻"),{target:{value:"1000001"}});
    expect(screen.getByTestId("大戶持股比例").textContent).not.toContain('"value":"70.000000000000"');
  });

  it("plots ownership only on source weekly dates, with a separate pp axis",async()=>{
    boot();fireEvent.click(screen.getByRole("tab",{name:"集保持股"}));
    const ratio=await screen.findByTestId("大戶持股比例");
    expect(ratio).toHaveAttribute("data-frequency","1w");
    expect(ratio.textContent).toContain('"date":"2026-01-09"');
    expect(ratio.textContent).not.toContain('"date":"2026-01-10"');
    expect(screen.getByTestId("持股比例週變化")).toBeVisible();
    expect(within(panel()).getByText(/週變化單位是百分點/)).toBeVisible();
    expect(within(panel()).getByRole("table")).toBeVisible();
    expect(screen.getByLabelText("級距分布統計日")).toHaveValue("2026-01-23");
    fireEvent.change(screen.getByLabelText("級距分布統計日"),{target:{value:"2026-01-09"}});
    expect(within(panel()).getByRole("table")).toHaveAccessibleName(/2026\/01\/09 持股級距分布/);
    expect(screen.getByTestId("大戶持股比例").textContent).toContain('"date":"2026-01-23"');
  });

  it("shows loading then safe errors and can retry",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockRejectedValueOnce(new ApiError(503,"api_unavailable","來源離線"));
    boot();await screen.findByText("籌碼 API 暫時無法連線");
    fireEvent.click(screen.getByRole("button",{name:"重新載入籌碼"}));
    await screen.findByTestId("法人每日買賣超");
    expect(api.getInstitutionalHistory).toHaveBeenCalledTimes(2);
  });

  it("empty data never becomes a zero chart",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockImplementation(async(_id,range)=>({...checked(institutionalHistoryFixture(),types.isInstitutionalHistoryResponse),...range,items:[],count:0}));
    boot();await screen.findByText("尚無已收錄三大法人資料");
    expect(screen.queryByTestId("法人每日買賣超")).not.toBeInTheDocument();
  });

  it("does not label older data stale without explicit source assessment",async()=>{
    boot();await screen.findByTestId("法人每日買賣超");
    expect(screen.queryByText("Stale · 來源已標示延遲")).not.toBeInTheDocument();
    expect(screen.getByText("時效未核實")).toBeVisible();
  });

  it("uses the metric quantity unit when history is unavailable",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockRejectedValue(new ApiError(503,"api_unavailable","資料暫時不可用"));
    vi.mocked(api.getInstitutionalMetrics).mockImplementation(async(_id,category,range)=>{
      const data=checked(institutionalMetricsFixture(category),types.isInstitutionalMetricsResponse);
      return {...data,...range,cumulative:data.cumulative.map(c=>({...c,quantity_unit:"units"}))};
    });
    boot();await screen.findByText("籌碼 API 暫時無法連線");
    const card=screen.getByText("5 交易日累計").closest("div")!;
    await waitFor(()=>expect(card).toHaveTextContent("80 單位"));
  });

  it("honors an explicit verified freshness window",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockImplementation(async(_id,range)=>{
      const data=checked(institutionalHistoryFixture(),types.isInstitutionalHistoryResponse);
      return {...data,...range,freshness:{...data.freshness,assessment:"within_expected_window"}};
    });
    boot();expect(await screen.findByText("來源預期更新範圍內")).toBeVisible();
    expect(screen.queryByText("時效未核實")).not.toBeInTheDocument();
  });

  it("renders explicit partial and stale states",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockImplementation(async(_id,range)=>{
      const data=checked(institutionalHistoryFixture(),types.isInstitutionalHistoryResponse);
      return {...data,...range,coverage:{...data.coverage,completeness:"partial"},freshness:{...data.freshness,assessment:"stale"}};
    });
    boot();expect(await screen.findByText("Partial · 來源欄位不完整")).toBeVisible();
    expect(screen.getByText("Stale · 來源已標示延遲")).toBeVisible();
  });

  it("rejects invalid dates before fetching another range",async()=>{
    boot();await screen.findByTestId("法人每日買賣超");
    const calls=vi.mocked(api.getInstitutionalHistory).mock.calls.length;
    fireEvent.change(screen.getByLabelText("籌碼開始日期"),{target:{value:"2026-01-24"}});
    fireEvent.click(screen.getByRole("button",{name:"套用籌碼日期"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("開始日期不可晚於結束日期");
    expect(api.getInstitutionalHistory).toHaveBeenCalledTimes(calls);
  });

  it("uses same chosen date range in each tab and preserves it",async()=>{
    boot();await screen.findByTestId("法人每日買賣超");
    fireEvent.click(screen.getByRole("button",{name:"近1個月"}));
    await waitFor(()=>expect(api.getInstitutionalHistory).toHaveBeenLastCalledWith(chipInstrumentFixture.id,{from:"2025-12-24",to:"2026-01-23"},expect.any(AbortSignal)));
    fireEvent.click(screen.getByRole("tab",{name:"融資融券"}));await screen.findByTestId("融資餘額");
    expect(api.getMarginHistory).toHaveBeenLastCalledWith(chipInstrumentFixture.id,{from:"2025-12-24",to:"2026-01-23"},expect.any(AbortSignal));
    expect(screen.getByLabelText("籌碼開始日期")).toHaveValue("2025-12-24");
  });

  it("keeps lender report kinds separate and exposes unavailable borrowed data",async()=>{
    vi.mocked(api.getLendingHistory).mockImplementation(async(_id,kind,range)=>{
      const data=checked(lendingHistoryFixture(kind),types.isLendingHistoryResponse);
      return kind==="borrowed_positions"?{...data,...range,items:[],count:0}:{...data,...range};
    });
    boot();fireEvent.click(screen.getByRole("tab",{name:"借券賣出"}));
    await screen.findByTestId("借券賣出餘額");
    fireEvent.change(screen.getByLabelText("借券報表類型"),{target:{value:"borrowed_positions"}});
    expect(await screen.findByText("尚無可用的借券未還報表")).toBeVisible();
    expect(screen.queryByTestId("借券未還餘額")).not.toBeInTheDocument();
  });

  it("supports keyboard tab navigation",async()=>{
    boot();const first=screen.getByRole("tab",{name:"三大法人"});first.focus();
    fireEvent.keyDown(first,{key:"ArrowRight"});
    expect(screen.getByRole("tab",{name:"集保持股"})).toHaveFocus();
    expect(screen.getByRole("tab",{name:"集保持股"})).toHaveAttribute("aria-selected","true");
    await screen.findByTestId("大戶持股比例");
  });
});
