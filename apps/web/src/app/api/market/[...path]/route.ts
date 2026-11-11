import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
const UUID = /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i;
const ERROR_CODES = new Set([
  "instrument_not_found",
  "stock_not_found",
  "invalid_search",
  "invalid_limit",
  "invalid_market",
  "invalid_instrument_type",
  "invalid_timeframe",
  "invalid_date",
  "invalid_range",
  "range_too_large",
  "too_many_input_bars",
  "too_many_bars",
  "invalid_request",
  "invalid_stored_data",
  "database_unavailable",
  "adjusted_price_unavailable",
  "invalid_indicator",
  "invalid_parameters",
  "calculation_limit_exceeded",
  "calculation_history_too_large",
  "too_many_indicator_points",
  "invalid_indicator_parameters",
  "invalid_price_basis",
  "ambiguous_trade_scope",
  "ambiguous_lending_channel",
  "unsupported_instrument_product",
  "invalid_date_range",
  "invalid_cursor",
  "query_limit_exceeded",
  "query_input_limit_exceeded",
  "invalid_stored_chip_data",
  "invalid_stored_ownership_data",
  "invalid_stored_margin_data",
  "invalid_stored_lending_data",
]);
function failure(status: number, code: string): Response {
  const message =
    status === 404
      ? "Resource not found."
      : status === 422
        ? "Request parameters are invalid."
        : "Market data is temporarily unavailable.";
  return Response.json(
    { detail: { code, message, errors: null } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}
function allowedQuery(path: string[]): readonly string[] | null {
  if (path.length === 1 && path[0] === "instruments")
    return [
      "q",
      "symbol",
      "name",
      "market",
      "instrument_type",
      "page",
      "page_size",
    ];
  if (path.length === 2 && path[0] === "instruments" && UUID.test(path[1]))
    return [];
  if (path.length === 3 && path[0] === "stocks" && UUID.test(path[1])) {
    if (path[2] === "bars") return ["timeframe", "from", "to", "limit"];
    if (path[2] === "latest") return [];
    if (path[2] === "indicators")
      return [
        "timeframe",
        "indicator",
        "parameters",
        "from",
        "to",
        "price_basis",
      ];
    if (path[2] === "institutional")
      return ["from", "to", "trade_scope", "limit", "cursor", "order"];
    if (path[2] === "ownership")
      return ["from", "to", "limit", "cursor", "order"];
    if (path[2] === "margin")
      return ["from", "to", "balance_scope", "limit", "cursor", "order"];
    if (path[2] === "lending")
      return ["from", "to", "report_kind", "trade_scope", "lending_channel", "channel_family", "limit", "cursor", "order"];
  }
  if (path.length === 4 && path[0] === "stocks" && UUID.test(path[1]) && path[3] === "metrics") {
    if (path[2] === "institutional") return ["from", "to", "trade_scope", "category", "period"];
    if (path[2] === "ownership") return ["from", "to", "threshold", "threshold_unit", "limit", "cursor", "order"];
  }
  if (path.length === 2 && path[0] === "data" && path[1] === "status")
    return ["limit"];
  return null;
}

/** Fixed upstream and read-only route allowlist; no public origin/URL parameter. */
export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const allowed = allowedQuery(path);
  if (!allowed) return failure(404, "route_not_found");
  const query = new URL(request.url).searchParams;
  if (
    query.toString().length > 2048 ||
    [...query.keys()].some(
      (key) => !allowed.includes(key) || query.getAll(key).length !== 1,
    )
  ) {
    return failure(422, "invalid_request");
  }
  try {
    const base = new URL(process.env.BACKEND_URL ?? "http://localhost:8000");
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    )
      return failure(503, "api_unavailable");
    const upstream = new URL(`/api/v1/${path.join("/")}`, base);
    upstream.search = query.toString();
    const response = await fetch(upstream, {
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(12000),
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      if (![404, 422, 503].includes(response.status))
        return failure(503, "api_unavailable");
      let code =
        response.status === 404
          ? "instrument_not_found"
          : response.status === 422
            ? "invalid_request"
            : "api_unavailable";
      try {
        const error: unknown = await response.json();
        if (
          typeof error === "object" &&
          error !== null &&
          "detail" in error &&
          typeof error.detail === "object" &&
          error.detail !== null &&
          "code" in error.detail &&
          typeof error.detail.code === "string" &&
          ERROR_CODES.has(error.detail.code)
        )
          code = error.detail.code;
      } catch {
        /* Never expose upstream HTML or internal exception text. */
      }
      return failure(response.status, code);
    }
    const data: unknown = await response.json();
    return Response.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return failure(503, "api_unavailable");
  }
}
