import { describe, expect, it } from "vitest";
import { changeDirection, dateLabel, decimal, recentRange, shares, timestamp } from "../src/lib/format";

describe("financial text formatting", () => {
  it("preserves decimal precision without Number rounding", () => {
    expect(decimal("1234567890123456.12345678")).toBe("1,234,567,890,123,456.12345678");
    expect(decimal("1E-8")).toBe("0.00000001");
    expect(decimal("0E-8")).toBe("0");
    expect(decimal(null)).toBe("—");
    expect(decimal("NaN")).toBe("—");
  });
  it("does not assign rising or falling colors to exponent zero", () => {
    expect(changeDirection("-0E-8")).toBe("neutral");
    expect(changeDirection(null)).toBe("neutral");
    expect(changeDirection("-1E-8")).toBe("down");
    expect(changeDirection("1E-8")).toBe("up");
  });
  it("never rounds an unsafe integer volume for display", () => {
    expect(shares(0)).toBe("0");
    expect(shares(12345)).toBe("12,345");
    expect(shares(Number.MAX_SAFE_INTEGER + 1)).toBe("—");
  });
  it("keeps market trade dates intact and localizes only timestamps", () => {
    expect(dateLabel("2026-01-02")).toBe("2026/01/02");
    expect(timestamp("2026-01-01T23:30:00Z", "Asia/Taipei")).toContain("2026/01/02");
  });
  it("bounds a query using stored coverage rather than assuming a trading session", () => {
    expect(recentRange("2020-01-01", "2026-01-05")).toEqual({from:"2025-01-05", to:"2026-01-05"});
    expect(recentRange("2026-01-05", "2026-01-05")).toEqual({from:"2026-01-05", to:"2026-01-05"});
    expect(recentRange(null, null)).toBeUndefined();
  });
});
