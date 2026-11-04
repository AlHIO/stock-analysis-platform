/**
 * Expand Pydantic Decimal JSON strings without rounding their coefficient.
 * Bounds cover the database's NUMERIC(24, 8)/NUMERIC(32, 8) values and aggregated
 * amounts while preventing untrusted exponents from allocating huge strings.
 */
export function expandDecimal(value: string): string | null {
  if (value.length > 140) return null;
  const match = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d{1,3}))?$/.exec(value);
  if (!match) return null;
  const [, sign, integer, fraction = "", rawExponent = "0"] = match;
  const digits = integer + fraction;
  const exponent = Number(rawExponent); // Bounded integer metadata, not a price.
  if (digits.length > 64 || Math.abs(exponent) > 64) return null;
  const position = integer.length + exponent;
  let whole: string;
  let decimal: string;
  if (position <= 0) {
    whole = "0";
    decimal = "0".repeat(-position) + digits;
  } else if (position >= digits.length) {
    whole = digits + "0".repeat(position - digits.length);
    decimal = "";
  } else {
    whole = digits.slice(0, position);
    decimal = digits.slice(position);
  }
  whole = whole.replace(/^0+(?=\d)/, "");
  const negative = sign === "-" && /[1-9]/.test(whole + decimal);
  return `${negative ? "-" : ""}${whole}${decimal ? `.${decimal}` : ""}`;
}
