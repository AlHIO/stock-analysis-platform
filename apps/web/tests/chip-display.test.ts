import { describe,it,expect } from "vitest";
import { alignDailySeries, calculationValue, chipRange, chipStatus, quantityValue, validChipRange } from "../src/lib/chip-display";
import type { ChipCalculation } from "../src/lib/chip-types";

describe("presentation without financial formulas",()=>{
  it("uses bounded calendar ranges across years",()=>{
    expect(chipRange("2026-01-23",30)).toEqual({from:"2025-12-24",to:"2026-01-23"});
    expect(validChipRange({from:"2025-01-23",to:"2026-01-23"},"2026-01-23")).toBeNull();
  });
  it.each([
    {from:"",to:"2026-01-23"},{from:"2026-02-30",to:"2026-03-01"},
    {from:"2026-01-24",to:"2026-01-23"},{from:"2026-01-22",to:"2026-01-24"},
    {from:"2024-01-01",to:"2026-01-23"},
  ])("rejects unsafe display range %j",range=>expect(validChipRange(range,"2026-01-23")).not.toBeNull());
  it("keeps true zero distinct from unavailable calculations",()=>{
    expect(calculationValue({value:"0",status:"complete"} as ChipCalculation)).toBe("0");
    expect(calculationValue({value:"0",status:"not_verified"} as ChipCalculation)).toBeNull();
    expect(quantityValue({value:null,unit:"shares",availability:"missing"})).toBeNull();
  });
  it("aligns price/daily flow only by observed date with whitespace",()=>{
    const aligned=alignDailySeries([{id:"price",label:"price",kind:"line",color:"green",points:[{date:"2026-01-02",value:"100"},{date:"2026-01-05",value:"101"}]},
      {id:"foreign",label:"foreign",kind:"line",color:"red",points:[{date:"2026-01-05",value:"0"}]}]);
    expect(aligned[1].points).toEqual([{date:"2026-01-02",value:null,status:"missing_data"},{date:"2026-01-05",value:"0"}]);
    expect(aligned[0].points).toHaveLength(2);
  });
  it("labels truncated/unsupported states without fake values",()=>{
    expect(chipStatus("truncated_history")).toContain("左端");
    expect(chipStatus("unsupported")).toContain("不支援");
  });
});
