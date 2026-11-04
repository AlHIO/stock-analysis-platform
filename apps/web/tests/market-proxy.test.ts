import { afterEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";
import { GET } from "../src/app/api/market/[...path]/route";
import { instrumentFixture } from "./fixtures/market-api";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
const call = (path: string[], search = "") => GET(new Request(`http://localhost:3000/api/market/${path.join("/")}${search}`) as NextRequest, { params: Promise.resolve({ path }) });

describe("read-only market proxy", () => {
  it("uses the server backend origin with an explicit API path and no cache", async () => {
    vi.stubEnv("BACKEND_URL", "http://backend:8000");
    const fetcher = vi.fn().mockResolvedValue(Response.json({ items: [] })); vi.stubGlobal("fetch", fetcher);
    const response = await call(["instruments"], "?q=2330&market=TWSE&instrument_type=stock");
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(fetcher.mock.calls[0][0].toString()).toBe("http://backend:8000/api/v1/instruments?q=2330&market=TWSE&instrument_type=stock");
    expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: "no-store", redirect: "error", signal: expect.any(AbortSignal) });
  });
  it.each([
    ["instruments", instrumentFixture.id], ["stocks", instrumentFixture.id, "latest"],
    ["stocks", instrumentFixture.id, "bars"], ["data", "status"],
  ])("allows the documented read endpoint %j", async (...path) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({})));
    expect((await call(path)).status).toBe(200);
  });
  it.each([["https:", "evil.example"], ["health"], ["stocks", "not-a-uuid", "bars"], ["instruments", ".."], ["stocks", instrumentFixture.id, "delete"]])("rejects unapproved paths %j", async (...path) => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await call(path)).status).toBe(404);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each(["?url=https://evil.example", "?q=a&q=b", `?q=${"x".repeat(2100)}`])("rejects unexpected or ambiguous query inputs", async query => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await call(["instruments"], query)).status).toBe(422);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each([404, 422, 503])("retains public HTTP %s while removing internal message text", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail: { code: "invalid_request", message: "postgresql://private", errors: [{ input: "sensitive" }] } }, { status })));
    const response = await call(["instruments"]);
    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ detail: { code: "invalid_request", message: status === 404 ? "Resource not found." : status === 422 ? "Request parameters are invalid." : "Market data is temporarily unavailable.", errors: null } });
  });
  it("sanitizes unknown backend errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private SQL exception", { status: 500 })));
    const response = await call(["instruments"]);
    expect(response.status).toBe(503); expect(await response.text()).not.toContain("SQL");
  });
  it.each([new TypeError("connection refused"), new DOMException("timeout", "TimeoutError")])("returns a safe outage response on connection failures", async error => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
    const response = await call(["instruments"]);
    expect(response.status).toBe(503); expect(await response.text()).not.toContain(error.message);
  });
  it.each(["file:///etc/passwd", "http://user:secret@backend:8000", "not a URL"])("rejects unsupported backend configuration safely", async url => {
    vi.stubEnv("BACKEND_URL", url); const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const response = await call(["instruments"]);
    expect(response.status).toBe(503); expect(fetcher).not.toHaveBeenCalled();
  });
});
