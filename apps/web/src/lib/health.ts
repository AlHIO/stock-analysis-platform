export type Readiness = { status: "ok" | "degraded"; dependencies: { postgres: "ok" | "error"; redis: "ok" | "error" } };
export async function getReadiness(baseUrl: string): Promise<Readiness | null> {
  try {
    const response = await fetch(`${baseUrl}/health/ready`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok && response.status !== 503) return null;
    const data: unknown = await response.json();
    if (!data || typeof data !== "object") return null;
    const value = data as Readiness;
    const valid = (status: unknown) => status === "ok" || status === "error";
    if ((value.status !== "ok" && value.status !== "degraded") || !valid(value.dependencies?.postgres) || !valid(value.dependencies?.redis)) return null;
    return value;
  } catch { return null; }
}
