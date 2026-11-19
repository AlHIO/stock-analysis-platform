/** SYNTHETIC subpane state/date tests; never production or provider data. */
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../src/lib/chip-api";
import { ApiError } from "../src/lib/market-api";
import { StockChipSubpanels } from "../src/components/stock-chip-subpanels";
import type { ChipSeries } from "../src/lib/chip-chart";
import { chipInstrumentFixture, institutionalHistoryFixture, ownershipMetricsFixture, marginHistoryFixture } from "./fixtures/chip";

vi.mock("../src/lib/chip-api",async original=>({...await original<typeof api>(),
  getInstitutionalHistory:vi.fn(),getOwnershipMetrics:vi.fn(),getMarginHistory:vi.fn(),getLendingHistory:vi.fn()}));
vi.mock("../src/components/chip-chart",()=>({ChipChart:({title,series,frequency,syncGroup}:{title:string;series:ChipSeries[];frequency:string;syncGroup:string})=><div data-testid={title} data-frequency={frequency} data-sync={syncGroup}>{JSON.stringify(series)}</div>}));
const range={from:"2026-01-01",to:"2026-01-23"};
function mount(selected=range){return render(<StockChipSubpanels id={chipInstrumentFixture.id} range={selected} syncGroup="SYNTHETIC-stock"/>);}
beforeEach(()=>{
  vi.clearAllMocks();
  vi.mocked(api.getInstitutionalHistory).mockResolvedValue(institutionalHistoryFixture() as unknown as Awaited<ReturnType<typeof api.getInstitutionalHistory>>);
  vi.mocked(api.getOwnershipMetrics).mockResolvedValue(ownershipMetricsFixture() as unknown as Awaited<ReturnType<typeof api.getOwnershipMetrics>>);
  vi.mocked(api.getMarginHistory).mockResolvedValue(marginHistoryFixture() as unknown as Awaited<ReturnType<typeof api.getMarginHistory>>);
});
afterEach(cleanup);

describe("SYNTHETIC K-line chip subpanes",()=>{
  it("loads nothing until enabled and reuses the existing chart renderer",async()=>{
    mount();expect(api.getInstitutionalHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox",{name:"法人買賣超"}));
    expect(await screen.findByTestId("法人每日買賣超")).toHaveAttribute("data-sync","SYNTHETIC-stock");
    expect(api.getInstitutionalHistory).toHaveBeenCalledTimes(1);
  });
  it("keeps weekly dates and does not create daily ownership observations",async()=>{
    mount();fireEvent.click(screen.getByRole("checkbox",{name:"集保大戶比例（週）"}));
    const chart=await screen.findByTestId("集保大戶持股比例");
    expect(chart).toHaveAttribute("data-frequency","1w");
    const series=JSON.parse(chart.textContent??"[]");
    expect(series[0].points.map((p:{date:string})=>p.date)).toEqual(ownershipMetricsFixture().items.map(p=>p.as_of_date));
    expect(series[0].points.some((p:{date:string})=>p.date==="2026-01-12")).toBe(false);
  });
  it("retains chosen parameters and cached requests when a pane is switched off",async()=>{
    mount();const toggle=screen.getByRole("checkbox",{name:"集保大戶比例（週）"});fireEvent.click(toggle);
    await screen.findByTestId("集保大戶持股比例");
    fireEvent.change(screen.getByRole("combobox",{name:"副圖大戶門檻"}),{target:{value:"1000001"}});
    await waitFor(()=>expect(api.getOwnershipMetrics).toHaveBeenCalledTimes(2));
    await screen.findByTestId("集保大戶持股比例");
    fireEvent.click(toggle);fireEvent.click(toggle);
    expect(screen.getByRole("combobox",{name:"副圖大戶門檻"})).toHaveValue("1000001");
    expect(api.getOwnershipMetrics).toHaveBeenCalledTimes(2);
  });
  it("leaves known missing values blank instead of zero",async()=>{
    const data=ownershipMetricsFixture();data.items[0].ratio.value=null;data.items[0].ratio.status="missing_data";
    vi.mocked(api.getOwnershipMetrics).mockResolvedValue(data as unknown as Awaited<ReturnType<typeof api.getOwnershipMetrics>>);
    mount();fireEvent.click(screen.getByRole("checkbox",{name:"集保大戶比例（週）"}));
    const chart=await screen.findByTestId("集保大戶持股比例");
    expect(JSON.parse(chart.textContent??"[]")[0].points[0].value).toBeNull();
    expect(screen.getByText(/部分比例不可取得/)).toBeVisible();
  });
  it("has error/retry states without retaining a successful result under new conditions",async()=>{
    vi.mocked(api.getInstitutionalHistory).mockRejectedValueOnce(new ApiError(503,"api_unavailable","no source"));
    mount();fireEvent.click(screen.getByRole("checkbox",{name:"法人買賣超"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("副圖 API 暫時無法連線");
    expect(screen.queryByTestId("法人每日買賣超")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"重試副圖"}));
    await screen.findByTestId("法人每日買賣超");
    expect(api.getInstitutionalHistory).toHaveBeenCalledTimes(2);
  });
  it("reports the backend stale flag and a bounded-query limitation",async()=>{
    const data=institutionalHistoryFixture();data.freshness.assessment="delayed";
    vi.mocked(api.getInstitutionalHistory).mockResolvedValue(data as unknown as Awaited<ReturnType<typeof api.getInstitutionalHistory>>);
    const view=mount();fireEvent.click(screen.getByRole("checkbox",{name:"法人買賣超"}));
    expect(await screen.findByText("Stale · 來源標示延遲。")).toBeVisible();
    view.rerender(<StockChipSubpanels id={chipInstrumentFixture.id} range={{from:"2016-01-01",to:"2026-01-23"}} syncGroup="SYNTHETIC-stock"/>);
    expect(screen.getByRole("status")).toHaveTextContent("每次最多顯示 366");
    expect(api.getInstitutionalHistory).toHaveBeenCalledTimes(1);
  });
});
