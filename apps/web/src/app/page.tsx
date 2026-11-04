import { SystemStatus } from "../components/system-status";
import { getReadiness } from "../lib/health";
export const dynamic = "force-dynamic";
export default async function Home() {
  const health = await getReadiness(process.env.BACKEND_URL ?? "http://localhost:8000");
  return <main className="mx-auto max-w-5xl px-6 py-20">
    <p className="text-sm font-semibold tracking-widest text-emerald-400">PHASE 0 · PROJECT FOUNDATION</p>
    <h1 className="mt-5 text-4xl font-bold tracking-tight sm:text-5xl">股票分析平台</h1>
    <p className="mt-6 max-w-2xl leading-8 text-slate-400">優先支援台灣上市與上櫃股票。此階段建立系統基礎，尚未串接行情資料。</p>
    <SystemStatus health={health} />
    <p className="mt-6 text-sm text-slate-500">狀態於頁面載入時檢查，重新整理可更新結果。</p>
  </main>;
}
