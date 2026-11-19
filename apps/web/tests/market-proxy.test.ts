import { afterEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";
import { GET } from "../src/app/api/market/[...path]/route";
import { instrumentFixture } from "./fixtures/market-api";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
const call = (path: string[], search = "") => GET(new Request(`http://localhost:3000/api/market/${path.join("/")}${search}`) as NextRequest, { params: Promise.resolve({ path }) });

describe("read-only market proxy", () => {
  it.each([
    ["stocks",instrumentFixture.id,"broker-flow"],
    ["stocks",instrumentFixture.id,"broker-flow","latest"],
    ["stocks",instrumentFixture.id,"broker-flow","top-branches"],
    ["stocks",instrumentFixture.id,"broker-flow","metrics"],
    ["broker-flow","status"],
  ])("allows canonical broker route %j",async(...path)=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(Response.json({})));
    expect((await call(path)).status).toBe(200);
  });
  it("forwards session requests only to broker TOP and keeps provider origin fixed",async()=>{
    const fetcher=vi.fn().mockResolvedValue(Response.json({}));vi.stubGlobal("fetch",fetcher);
    const response=await call(["stocks",instrumentFixture.id,"broker-flow","top-branches"],"?sessions=5&top_n=15&side=net_buyers");
    expect(response.status).toBe(200);
    expect(new URL(fetcher.mock.calls[0][0]).searchParams.get("sessions")).toBe("5");
    expect((await call(["stocks",instrumentFixture.id,"broker-flow","metrics"],"?sessions=5")).status).toBe(422);
  });
  it.each(["?source_url=https://evil.example","?source=a&source=b","?trade_scope=a&trade_scope=b","?calendar_verified=true","?algorithm_parameters="+"x".repeat(4097)])("rejects unsafe broker parameters before upstreamfetch",async query=>{
    const fetcher=vi.fn();vi.stubGlobal("fetch",fetcher);
    expect((await call(["stocks",instrumentFixture.id,"broker-flow","metrics"],query)).status).toBe(422);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("accepts the H5 bounded JSON parameter payload without widening unrelated endpoints",async()=>{
    const fetcher=vi.fn().mockResolvedValue(Response.json({}));vi.stubGlobal("fetch",fetcher);
    const query="?algorithm_parameters="+encodeURIComponent(JSON.stringify({label:"x".repeat(2200)}));
    expect((await call(["stocks",instrumentFixture.id,"broker-flow","metrics"],query)).status).toBe(200);
    expect((await call(["instruments"],"?q="+"x".repeat(2200))).status).toBe(422);
  });
  it("retains safe broker budget error codes without internal diagnostics",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(Response.json({detail:{code:"broker_query_limit_exceeded",message:"postgresql://secret",errors:["SECRET"]}},{status:422})));
    const response=await call(["stocks",instrumentFixture.id,"broker-flow"]);
    expect(await response.json()).toEqual({detail:{code:"broker_query_limit_exceeded",message:"Request parameters are invalid.",errors:null}});
  });
  it.each([
    ["institutional"], ["institutional","metrics"], ["ownership"],
    ["ownership","metrics"], ["margin"], ["lending"],
  ])("allows only documented chip read paths %j",async(...suffix)=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(Response.json({})));
    expect((await call(["stocks",instrumentFixture.id,...suffix],"?from=2026-01-01&to=2026-01-31")).status).toBe(200);
  });
  it.each(["?report_kind=borrowed_positions&report_kind=lending_short_sales","?url=https://evil.example","?source_url=https://evil.example"])("rejects unsafe chip query inputs",async(query)=>{
    const fetcher=vi.fn();vi.stubGlobal("fetch",fetcher);
    expect((await call(["stocks",instrumentFixture.id,"lending"],query)).status).toBe(422);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("preserves a public source ambiguity code but removes internal diagnostics",async()=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(Response.json({detail:{code:"ambiguous_lending_channel",message:"private SQL/DSN",errors:["SECRET"]}},{status:422})));
    const response=await call(["stocks",instrumentFixture.id,"lending"]);
    expect(await response.json()).toEqual({detail:{code:"ambiguous_lending_channel",message:"Request parameters are invalid.",errors:null}});
  });
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
