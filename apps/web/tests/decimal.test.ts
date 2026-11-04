import { describe, expect, it } from "vitest";
import { expandDecimal } from "../src/lib/decimal";
import { decimal } from "../src/lib/format";

describe("Pydantic decimal string expansion", () => {
  it.each([
    ["1E-8", "0.00000001"], ["0E-8", "0.00000000"],
    ["-1.25e-3", "-0.00125"], ["+1.25E+3", "1250"],
    ["123.4500", "123.4500"], ["1E+16", "10000000000000000"],
    ["1234567890123456.12345678", "1234567890123456.12345678"],
    ["-0E-8", "0.00000000"],
  ])("expands %s exactly", (input, expected) => {
    expect(expandDecimal(input)).toBe(expected);
  });
  it.each(["", "NaN", "Infinity", "1E99999999", "1E-99999999", "1e65", "1e-65", "1e", " 1E-8", "1.2.3", "1".repeat(65)])("rejects malformed or excessive values: %s", input => {
    expect(expandDecimal(input)).toBeNull();
    expect(decimal(input)).toBe("—");
  });
  it("formats expanded positive, zero and negative financial values without floating point rounding", () => {
    expect(decimal("1E-8")).toBe("0.00000001");
    expect(decimal("0E-8")).toBe("0");
    expect(decimal("-1.25E+3")).toBe("-1,250");
    expect(decimal("-1234567890123456.12345678")).toBe("-1,234,567,890,123,456.12345678");
    expect(decimal(null)).toBe("—");
  });
});
