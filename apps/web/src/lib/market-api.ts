import {
  isBarsResponse,
  isInstrument,
  isInstrumentPage,
  isLatestResponse,
} from "./market-types";
import type { Timeframe } from "./market-types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function errorMessage(status: number): string {
  if (status === 404) return "找不到這檔股票，請重新搜尋。";
  if (status === 422) return "查詢條件無效或範圍過大，請調整後再試。";
  if (status === 503 || status === 502 || status === 504)
    return "行情 API 暫時無法連線，請稍後重試。";
  return "讀取資料失敗，請稍後重試。";
}

export async function request<T>(
  path: string,
  guard: (data: unknown) => data is T,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/market/${path}`, {
      cache: "no-store",
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(15000)])
        : AbortSignal.timeout(15000),
      headers: { Accept: "application/json" },
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(503, "api_unavailable", errorMessage(503));
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError(
      response.ok ? 502 : response.status,
      "invalid_response",
      errorMessage(response.ok ? 502 : response.status),
    );
  }
  if (!response.ok) {
    const code =
      typeof data === "object" &&
      data !== null &&
      "detail" in data &&
      typeof data.detail === "object" &&
      data.detail !== null &&
      "code" in data.detail &&
      typeof data.detail.code === "string" &&
      /^[a-z_]{1,64}$/.test(data.detail.code)
        ? data.detail.code
        : "request_failed";
    throw new ApiError(response.status, code, errorMessage(response.status));
  }
  if (!guard(data))
    throw new ApiError(
      502,
      "invalid_response",
      "行情 API 回傳的資料格式無法安全顯示，請稍後重試。",
    );
  return data;
}

export function searchInstruments(
  query: string,
  market: string,
  signal?: AbortSignal,
  page = 1,
) {
  const params = new URLSearchParams({
    q: query.trim(),
    page_size: "20",
    instrument_type: "stock",
  });
  if (page !== 1) params.set("page", String(page));
  if (market && market !== "ALL") params.set("market", market);
  return request(`instruments?${params}`, isInstrumentPage, signal);
}
export function getInstrument(id: string, signal?: AbortSignal) {
  return request(`instruments/${encodeURIComponent(id)}`, isInstrument, signal);
}
export function getLatest(id: string, signal?: AbortSignal) {
  return request(
    `stocks/${encodeURIComponent(id)}/latest`,
    isLatestResponse,
    signal,
  );
}
export function getBars(
  id: string,
  timeframe: Timeframe,
  signal?: AbortSignal,
  range?: { from?: string; to?: string },
) {
  const params = new URLSearchParams({ timeframe });
  if (range?.from) params.set("from", range.from);
  if (range?.to) params.set("to", range.to);
  return request(
    `stocks/${encodeURIComponent(id)}/bars?${params}`,
    isBarsResponse,
    signal,
  );
}
