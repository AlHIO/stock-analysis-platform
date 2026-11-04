import { ApiError, request } from "./market-api";
import type { Timeframe } from "./market-types";
import { isIndicatorResponse, type IndicatorName } from "./technical-types";

export function getIndicator(
  id: string,
  timeframe: Timeframe,
  indicator: IndicatorName,
  parameters: Record<string, number>,
  range: { from: string; to: string },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    timeframe,
    indicator,
    parameters: JSON.stringify(parameters),
    from: range.from,
    to: range.to,
    price_basis: "raw",
  });
  return request(
    `stocks/${encodeURIComponent(id)}/indicators?${query}`,
    isIndicatorResponse,
    signal,
  ).then((data) => {
    if (
      data.instrument.id.toLowerCase() !== id.toLowerCase() ||
      data.timeframe !== timeframe ||
      data.indicator !== indicator ||
      data.from !== range.from ||
      data.to !== range.to ||
      Object.entries(parameters).some(
        ([key, value]) => data.parameters[key] !== value,
      )
    ) {
      throw new ApiError(
        502,
        "invalid_response",
        "指標回應與查詢條件不符，請稍後重試。",
      );
    }
    return data;
  });
}
