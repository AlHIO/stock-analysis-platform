import { afterEach, describe, expect, it, vi } from "vitest";
import { getReadiness } from "../src/lib/health";
afterEach(() => vi.unstubAllGlobals());
describe("backend readiness", () => {
  it("reads a healthy backend without caching", async () => {
    const data = { status: "ok", dependencies: { postgres: "ok", redis: "ok" } };
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => data });
    vi.stubGlobal("fetch", fetcher);
    expect(await getReadiness("http://backend:8000")).toEqual(data);
    expect(fetcher).toHaveBeenCalledWith("http://backend:8000/health/ready", expect.objectContaining({ cache: "no-store" }));
  });
  it("preserves dependency failure details on 503", async () => {
    const data = { status: "degraded", dependencies: { postgres: "error", redis: "ok" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => data }));
    expect(await getReadiness("http://backend:8000")).toEqual(data);
  });
  it.each([new Error("offline"), new DOMException("Timed out", "TimeoutError")])("handles connection failures", async (error) => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
    expect(await getReadiness("http://backend:8000")).toBeNull();
  });
  it("rejects malformed responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ok" }) }));
    expect(await getReadiness("http://backend:8000")).toBeNull();
  });
});
