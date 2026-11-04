import type { Readiness } from "../lib/health";
export function SystemStatus({ health }: { health: Readiness | null }) {
  const services = [
    ["FastAPI", health ? "已連線" : "無法連線"],
    ["PostgreSQL", health?.dependencies.postgres === "ok" ? "已連線" : "尚未就緒"],
    ["Redis", health?.dependencies.redis === "ok" ? "已連線" : "尚未就緒"],
  ];
  return <section aria-label="系統服務狀態" className="mt-10 grid gap-4 sm:grid-cols-3">
    {services.map(([name, status]) => <div key={name} className="rounded-xl border border-slate-700 bg-slate-900 p-6">
      <h2 className="text-lg font-semibold">{name}</h2>
      <p className={`mt-3 ${status === "已連線" ? "text-emerald-400" : "text-amber-300"}`}>{status}</p>
    </div>)}
  </section>;
}
