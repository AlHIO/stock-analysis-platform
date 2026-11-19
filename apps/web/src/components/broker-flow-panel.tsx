"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../lib/market-api";
import { getBrokerHistory, getBrokerLatest, getBrokerTopBranches, getBrokerMetrics } from "../lib/chip-api";
import type { ChipRange } from "../lib/chip-types";
import { brokerDailyPoints, brokerRankRows } from "../lib/broker-types";
import type { ChipSeries } from "../lib/chip-chart";
import { chipStatus } from "../lib/chip-display";
import { changeDirection, dateLabel, decimal, timestamp } from "../lib/format";
import { ChipChart } from "./chip-chart";
import styles from "./broker-flow-panel.module.css";

type History = Awaited<ReturnType<typeof getBrokerHistory>>;
type MetricPage = Awaited<ReturnType<typeof getBrokerMetrics>>;
type MetricResult = MetricPage["result"];
type Report = History["items"][number];
type Selection = { trade_scope?: string; source?: string };
type Sessions = 1 | 5 | 10 | 20 | 60;
type Period = Sessions | "custom";
type Resource<T> = { key: string; revision: number; status: "loading" | "ready" | "error"; data?: T; error?: unknown };
type ScopeOption = { id: string; source: { id: string; name: string; code: string }; code: string; version: string; description: string };
type Obj = Record<string, unknown>;
const object = (value: unknown): value is Obj => value !== null && typeof value === "object" && !Array.isArray(value);
const metricValue = (value?: MetricResult): string | null => value?.status === "complete" && typeof value.value === "string" ? value.value : null;
const quantityUnit = (unit: string | undefined) => unit === "shares" ? "股" : unit === "units" ? "單位" : "來源單位";

/** Match immutable input facts across independent GET snapshots, not query clocks. */
function brokerRevisionKey(result: MetricResult | undefined): string | null {
  if (!result) return null;
  const keys: string[] = [];
  for (const ref of result.input_refs) {
    if (ref.product !== "broker_flow") continue;
    const scope = ref.metadata.scope;
    if (!object(scope) || typeof scope.id !== "string") return null;
    keys.push(JSON.stringify([ref.statistics_date, ref.source_id, ref.revision_id, ref.source_contract_version, scope.id]));
  }
  return JSON.stringify([...new Set(keys)].sort());
}

/** Data belongs to one exact selection; a pending request never relabels old results. */
function useBrokerResource<T>(key: string, active: boolean, loader: (signal: AbortSignal) => Promise<T>) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<Resource<T>>({ key: "", revision: 0, status: "loading" });
  const current = useRef(state); current.current = state;
  const load = useRef(loader); load.current = loader;
  useEffect(() => {
    if (!active || current.current.key === key && current.current.revision === revision && current.current.status !== "loading") return;
    const controller = new AbortController();
    setState({ key, revision, status: "loading" });
    load.current(controller.signal).then(data => {
      if (!controller.signal.aborted) setState({ key, revision, status: "ready", data });
    }).catch(error => {
      if (!controller.signal.aborted) setState({ key, revision, status: "error", error });
    });
    return () => controller.abort();
  }, [key, active, revision]);
  return { ...state, data: state.key === key ? state.data : undefined,
    status: state.key === key ? state.status : "loading" as const,
    retry: () => setRevision(value => value + 1) };
}

function ResourceState({ state, label }: { state: { status: string; error?: unknown; retry: () => void }; label: string }) {
  if (state.status === "loading") return <p role="status" className={styles.loading}>正在載入{label}…</p>;
  if (state.status !== "error") return null;
  const invalid = state.error instanceof ApiError && state.error.status < 500;
  return <div role="alert" className={styles.warning}><strong>{invalid ? `${label}查詢尚無法完成` : "分點 API 暫時無法連線"}</strong>
    <p>{state.error instanceof ApiError ? state.error.message : "請稍後重試。"}</p><button onClick={state.retry}>重新載入{label}</button></div>;
}

function Formula({ result }: { result?: MetricResult }) {
  if (!result) return null;
  return <details className={styles.formula}><summary>計算公式與版本</summary><p>{result.formula ?? "此資料來源不支援該指標。"}</p>
    <p>{result.metric} · {result.algorithm_version ?? "無可用算法版本"}</p>
    <p>{result.reasons.join(" · ") || "使用後端計算結果，前端不重算金融公式。"}</p>
    <p>輸入報表 {result.input_refs.length} 份 · {result.provisional ? "Provisional：發布時間未完整核實" : "保留來源可用時間"}</p>
    {result.input_refs.length > 0 && <ul>{result.input_refs.slice(0, 5).map(ref => <li key={`${ref.revision_id}/${ref.statistics_date}`}>
      統計日 {dateLabel(ref.statistics_date)} · Revision {ref.revision_id} · 發布 {ref.source_published_at ? timestamp(ref.source_published_at) : "未知"}
    </li>)}</ul>}
    {result.input_refs.length > 5 && <p>此處列出前 5 份來源參照；本結果共使用 {result.input_refs.length} 份報表。</p>}</details>;
}

function Card({ label, value, unit, status, children }: { label: string; value?: string | null; unit?: string; status?: string; children?: ReactNode }) {
  return <div className={styles.card}><span>{label}</span><strong className={styles[changeDirection(value)]}>{decimal(value)}{value != null && unit && <small> {unit}</small>}</strong>
    <small>{status ? chipStatus(status) : value == null ? "Not Available" : "後端計算結果"}</small>{children}</div>;
}

function scopeOption(report: Report): ScopeOption {
  return { id: report.scope.id, source: report.lineage.source, code: report.scope.code,
    version: report.scope.version, description: report.scope.description };
}

function branchNames(result: MetricResult | undefined, history: Report[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const report of [...history].reverse()) {
    for (const trade of report.trades) names.set(trade.branch_id, trade.identity.branch_name);
  }
  // Identity evidence comes from the backend; no name-to-identity matching.
  for (const ref of result?.input_refs ?? []) {
    if (!Array.isArray(ref.metadata.identities)) continue;
    for (const identity of ref.metadata.identities) {
      if (object(identity) && typeof identity.branch_id === "string" && typeof identity.branch_name === "string") {
        names.set(identity.branch_id, identity.branch_name);
      }
    }
  }
  return names;
}

function Ranking({ title, result, scope, names, onBranch }: { title: string; result?: MetricResult; scope?: ScopeOption; names: Map<string, string>; onBranch: (id: string) => void }) {
  const rows = result ? brokerRankRows(result) : [];
  const unit = quantityUnit(result?.unit ?? undefined);
  return <div className={styles.ranking}>
    <h4>{title}</h4>
    {result && result.status !== "complete" && <p className={styles.notice}>Not Available · {chipStatus(result.status)}。未取得核實完整區間時，不使用部分分點冒充完整排行榜。</p>}
    {result?.status === "complete" && rows.length === 0 && <p className={styles.notice}>此完整來源口徑沒有符合方向的分點；不以零值分點補榜。</p>}
    {rows.length > 0 && <p className={styles.note}>表格可左右捲動查看完整買進、賣出與買賣超。</p>}
    {rows.length > 0 && <div className={styles.tableWrap}><table><caption>{title} · {scope?.code ?? "來源口徑"}／{scope?.version ?? "—"}</caption>
      <thead><tr><th>排名</th><th>券商分點</th><th>買進（{unit}）</th><th>賣出（{unit}）</th><th>買賣超（{unit}）</th><th>成交口徑</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.branch_id}><td>{row.rank}</td><th><button className={styles.branchLink} onClick={() => onBranch(row.branch_id)}>{names.get(row.branch_id) ?? "身分名稱尚未提供"}</button><small>{row.branch_id}</small></th>
        <td>{decimal(row.buy_quantity)}</td><td>{decimal(row.sell_quantity)}</td><td className={styles[changeDirection(row.net_quantity)]}>{decimal(row.net_quantity)}</td><td>{scope?.code ?? "—"}</td></tr>)}</tbody>
    </table></div>}
    <Formula result={result}/>
  </div>;
}

export function BrokerFlowPanel({ id, range, active, currency, syncGroup }: {
  id: string; range: ChipRange; active: boolean; currency: string; syncGroup?: string;
}) {
  const [period, setPeriod] = useState<Period>(1);
  const [scopeId, setScopeId] = useState("");
  const [branch, setBranch] = useState("");
  const [validationRevision, setValidationRevision] = useState(0);
  const scopes = useRef(new Map<string, ScopeOption>());
  const baseKey = JSON.stringify([id, range]);
  const discovery = useBrokerResource(baseKey, active,
    signal => getBrokerHistory(id, range, { limit: 10, order: "desc" }, signal));
  for (const report of discovery.data?.items ?? []) scopes.current.set(report.scope.id, scopeOption(report));
  const chosen = scopeId ? scopes.current.get(scopeId) : scopes.current.size === 1 ? [...scopes.current.values()][0] : undefined;
  const selection: Selection = chosen ? { trade_scope: chosen.id, source: chosen.source.id } : {};
  const latestKey = JSON.stringify([id, selection]);
  const latest = useBrokerResource(latestKey, active, signal => getBrokerLatest(id, selection, signal));
  if (latest.data?.point) scopes.current.set(latest.data.point.scope.id, scopeOption(latest.data.point));
  const effectiveScope = chosen ?? (latest.data?.point ? scopeOption(latest.data.point) : undefined);
  const exactSelection: Selection = effectiveScope ? { trade_scope: effectiveScope.id, source: effectiveScope.source.id } : {};
  const reports = (discovery.data?.items ?? []).filter(report => report.scope.id === effectiveScope?.id);
  const latestPoint = latest.data?.point;
  const last = latestPoint && latestPoint.scope.id === effectiveScope?.id ? latestPoint : reports[0];
  const latestInRange = last && last.trade_date <= range.to ? last.trade_date : reports.find(report => report.trade_date <= range.to)?.trade_date;
  const rankRange = period === 1 && latestInRange ? { from: latestInRange, to: latestInRange } : range;
  const rankingEnabled = active && !!effectiveScope && (period !== 1 || !!latestInRange);
  const rankKey = JSON.stringify([id, rankRange, exactSelection, period, validationRevision]);
  const topOptions = { ...exactSelection, top_n: 15 as const, ...(period !== "custom" && period !== 1 ? { sessions: period } : {}) };
  const buyers = useBrokerResource(`${rankKey}/buyers`, rankingEnabled,
    signal => getBrokerTopBranches(id, rankRange, { ...topOptions, side: "net_buyers" }, signal));
  const sellers = useBrokerResource(`${rankKey}/sellers`, rankingEnabled,
    signal => getBrokerTopBranches(id, rankRange, { ...topOptions, side: "net_sellers" }, signal));
  const buyResult = buyers.data?.result;
  const sellResult = sellers.data?.result;
  const buyRevisionKey = brokerRevisionKey(buyResult);
  const sellRevisionKey = brokerRevisionKey(sellResult);
  const rankInputsMatch = buyRevisionKey !== null && buyRevisionKey === sellRevisionKey;
  const rankRevisionMismatch = buyResult?.status === "complete" && sellResult?.status === "complete" && !rankInputsMatch;
  const sessionWindow = buyResult?.details.session_window;
  const intervalConfirmed = period === "custom" || period === 1 || object(sessionWindow) && sessionWindow.verified === true;
  const analysisRange = buyResult?.status === "complete" && sellResult?.status === "complete" && rankInputsMatch && intervalConfirmed &&
    buyResult.request.start === sellResult.request.start && buyResult.request.end === sellResult.request.end
    ? { from: buyResult.request.start, to: buyResult.request.end } : null;
  // Hidden visited tabs retain their rendered charts and settings; only fetching pauses.
  const metricsEnabled = analysisRange !== null && effectiveScope !== undefined;
  const metricRange = analysisRange ?? rankRange;
  const metricKey = JSON.stringify([id, metricRange, exactSelection, period, !!analysisRange, buyRevisionKey, validationRevision]);
  const proxy = useBrokerResource(`${metricKey}/proxy`, active && metricsEnabled, signal => getBrokerMetrics(id,
    "proxy.broker_top_n_net_imbalance", metricRange, { top_n: 15, selection: "period_aggregate" }, exactSelection, signal));
  const dailyProxy = useBrokerResource(`${metricKey}/daily-proxy`, active && metricsEnabled, signal => getBrokerMetrics(id,
    "proxy.broker_daily_top_n_imbalance_sum", metricRange, { top_n: 15, selection: "daily_reselect" }, exactSelection, signal));
  const activeCounts = useBrokerResource(`${metricKey}/active`, active && metricsEnabled, signal => getBrokerMetrics(id,
    "broker.active_buy_sell_count_difference", metricRange, {}, exactSelection, signal));
  const netCounts = useBrokerResource(`${metricKey}/net`, active && metricsEnabled, signal => getBrokerMetrics(id,
    "broker.net_buy_sell_count_difference", metricRange, {}, exactSelection, signal));
  const branchMetric = useBrokerResource(`${metricKey}/branch/${branch}`, active && metricsEnabled && branch !== "", signal => getBrokerMetrics(id,
    period === "custom" ? "broker.branch_range_net_quantity" : "broker.branch_session_cumulative_net", metricRange,
    { branch_id: branch, ...(period !== "custom" ? { sessions: period } : {}) }, exactSelection, signal));
  const names = useMemo(() => {
    const found = branchNames(buyResult, reports);
    for (const [identifier, name] of branchNames(sellResult, reports)) if (!found.has(identifier)) found.set(identifier, name);
    return found;
  }, [buyResult, sellResult, discovery.data, effectiveScope?.id]);
  const unit = quantityUnit(proxy.data?.result.unit ?? last?.quantity_unit);
  const proxyResult = metricsEnabled ? proxy.data?.result : undefined;
  const mismatchesRankInputs = (result: MetricResult | undefined) => metricsEnabled && result?.status === "complete" &&
    (buyRevisionKey === null || brokerRevisionKey(result) !== buyRevisionKey);
  const proxyRevisionMismatch = mismatchesRankInputs(proxyResult);
  const dailyRevisionMismatch = mismatchesRankInputs(dailyProxy.data?.result);
  const activeRevisionMismatch = mismatchesRankInputs(activeCounts.data?.result);
  const netRevisionMismatch = mismatchesRankInputs(netCounts.data?.result);
  const branchRevisionMismatch = branch !== "" && mismatchesRankInputs(branchMetric.data?.result);
  const proxyNet = proxyRevisionMismatch ? null : metricValue(proxyResult);
  const direction = changeDirection(proxyNet);
  const selectedBuy = !proxyRevisionMismatch && proxyResult?.status === "complete" && typeof proxyResult.details.selected_buy_quantity === "string" ? proxyResult.details.selected_buy_quantity : null;
  const selectedSell = !proxyRevisionMismatch && proxyResult?.status === "complete" && typeof proxyResult.details.selected_sell_quantity === "string" ? proxyResult.details.selected_sell_quantity : null;
  const activeSeries = metricsEnabled && !activeRevisionMismatch && activeCounts.data ? brokerDailyPoints(activeCounts.data.result) : [];
  const netSeries = metricsEnabled && !netRevisionMismatch && netCounts.data ? brokerDailyPoints(netCounts.data.result) : [];
  const flowSeries: ChipSeries[] = [{ id: "broker-proxy", label: "每日重選 TOP 15 分點 Proxy", color: "#ed7784", kind: "histogram",
    points: metricsEnabled && !dailyRevisionMismatch && dailyProxy.data ? brokerDailyPoints(dailyProxy.data.result) : [] }];
  const lastPoint = last ?? latest.data?.point ?? null;
  const isEmpty = discovery.status === "ready" && !discovery.data?.items.length && latest.status === "ready" && latest.data?.point === null;
  const stale = ["stale", "delayed"].includes(String(latest.data?.freshness.assessment ?? discovery.data?.freshness.assessment));
  const dateCaption = analysisRange ?? rankRange;

  return <div className={styles.panel}>
    <header className={styles.header}><div><span className={styles.eyebrow}>BROKER FLOW · EXPLAINABLE PROXY</span><h3>主力分點分析</h3>
      <p>分點是交易中介，不能識別個別投資人；買賣超不代表真實庫存變化。</p></div>
      <label>來源與成交口徑<select aria-label="分點來源與成交口徑" value={effectiveScope?.id ?? ""} onChange={event => { setScopeId(event.target.value); setBranch(""); }}>
        <option value="">請選擇單一來源口徑</option>{[...scopes.current.values()].map(scope => <option key={scope.id} value={scope.id}>{scope.source.name} · {scope.code}／{scope.version}</option>)}
      </select></label></header>
    <div className={styles.periods} role="group" aria-label="分點排行榜期間">{([1, 5, 10, 20, 60, "custom"] as const).map(value =>
      <button key={value} aria-pressed={period === value} onClick={() => setPeriod(value)}>{value === "custom" ? "自訂區間" : value === 1 ? "1 日" : `${value} 個交易日`}</button>)}</div>
    <button className={styles.recheck} type="button" onClick={() => setValidationRevision(value => value + 1)}>重新核對本區間</button>
    <p className={styles.note}>查詢區間 {dateLabel(dateCaption.from)} — {dateLabel(dateCaption.to)}。{period === "custom" ? "使用上方籌碼日期範圍；不是固定交易日數。" : period === 1 ? "單日使用來源真正的已保存交易日。" : "交易日範圍由後端核實日曆選取；未核實時為 Not Available。"}</p>
    {effectiveScope && <p className={styles.note}>交易口徑說明：{effectiveScope.description}</p>}
    <ResourceState state={discovery} label="分點資料"/>
    {latest.status === "error" && <ResourceState state={latest} label="最新分點"/>}
    {isEmpty && <div role="status" className={styles.empty}><strong>Unavailable · 尚無已收錄券商分點資料</strong><p>不建立假排行榜、主力柱狀圖或每日大戶進出。</p></div>}
    {!effectiveScope && !isEmpty && discovery.status === "ready" && <p className={styles.notice}>請明確選擇來源與成交口徑；不同來源、一般／零股／鉅額資料不自動合併。</p>}
    <aside className={styles.status} aria-label="分點資料狀態"><div className={styles.badges}><span>已保存日報 · 非即時</span>
      {lastPoint && <span>{chipStatus(lastPoint.quality.status)}</span>}<span>{stale ? "Stale · 來源已標示延遲" : "時效未核實"}</span>
      {(latest.data?.coverage.completeness === "not_verified" || discovery.data?.coverage.completeness === "not_verified") && <span>歷史覆蓋與交易日曆尚未核實</span>}
      {(discovery.data?.next_cursor || discovery.data?.coverage.truncated) && <span>來源預覽有分頁 · 未宣稱完整歷史</span>}
      {lastPoint?.quality.provisional && <span>發布時間未知 · Provisional</span>}</div>
      <dl><div><dt>最新已保存統計日</dt><dd>{dateLabel(lastPoint?.trade_date)}</dd></div><div><dt>資料來源／口徑</dt><dd>{effectiveScope ? `${effectiveScope.source.name} · ${effectiveScope.code}／${effectiveScope.version}` : "尚無可選來源"}</dd></div>
        <div><dt>最新報表官方發布時間</dt><dd>{lastPoint?.timing.source_published_at ? timestamp(lastPoint.timing.source_published_at) : "未知，未自行推算"}</dd></div><div><dt>最新報表首次觀察／匯入</dt><dd>{timestamp(lastPoint?.timing.first_seen_at)} / {timestamp(lastPoint?.timing.ingested_at)}</dd></div></dl>
      {lastPoint && <details><summary>最新已保存報表來源與 Revision</summary><p>Revision：{lastPoint.lineage.revision_id}</p><p>Contract：{lastPoint.lineage.source_contract_version}</p><p>Raw／Run：{lastPoint.lineage.raw_payload_id} / {lastPoint.lineage.run_id}</p></details>}
      <p>正式下載及公開／衍生展示授權尚未核實。以下是合法保存資料的內部查詢，不代表取得公開展示許可。</p></aside>
    {rankingEnabled && <><ResourceState state={buyers} label="買超排行榜"/><ResourceState state={sellers} label="賣超排行榜"/></>}
    {rankRevisionMismatch && <div role="alert" className={styles.warning}><strong>買超與賣超榜的輸入 Revision 不一致</strong><p>資料可能在兩次查詢之間修訂。本次不將它們當作同一完整區間計算摘要，請重新核對本區間。</p></div>}
    {proxyRevisionMismatch && <div role="alert" className={styles.warning}><strong>Proxy 與排行榜的輸入 Revision 不一致</strong><p>暫不合併顯示買進、賣出與方向摘要；請重新核對本區間。</p></div>}
    {(dailyRevisionMismatch || activeRevisionMismatch || netRevisionMismatch || branchRevisionMismatch) && <div role="alert" className={styles.warning}><strong>部分指標的輸入 Revision 已變更</strong><p>不同版本的觀察不拼成一致區間；對應摘要或圖表暫留白，請重新核對本區間。</p></div>}
    <div className={styles.direction}><span>區間 TOP 15 分點淨額不平衡 Proxy</span><strong className={styles[direction]}>{proxyNet == null ? "Not Available" : direction === "up" ? "偏買超（Proxy）" : direction === "down" ? "偏賣超（Proxy）" : "淨額平衡（不代表無交易）"}</strong><p>這是有公式與版本的代理指標，不是官方主力或真實中實戶買賣超。</p></div>
    {metricsEnabled && <ResourceState state={proxy} label="分點 Proxy"/>}
    <div className={styles.cards}>
      <Card label="選中分點買進" value={selectedBuy} unit={unit} status={proxyRevisionMismatch ? "incompatible_data" : proxyResult?.status}><small>來自後端選中分點聯集的實際買進；不以正 Net 代替。</small></Card>
      <Card label="選中分點賣出" value={selectedSell} unit={unit} status={proxyRevisionMismatch ? "incompatible_data" : proxyResult?.status}><small>來自後端選中分點聯集的實際賣出；不以負 Net 代替。</small></Card>
      <Card label="TOP 15 Proxy 淨額" value={proxyNet} unit={unit} status={proxyRevisionMismatch ? "incompatible_data" : proxyResult?.status}><Formula result={proxyResult}/></Card>
      <Card label="張數換算" value={null}><small>未提供該商品／日期的核實換算係數，不假設一張 1,000 股。金額幣別：{lastPoint?.currency ?? currency}。</small></Card>
    </div>
    <div className={styles.rankings}><Ranking title="TOP 15 買超分點" result={buyResult} scope={effectiveScope} names={names} onBranch={setBranch}/>
      <Ranking title="TOP 15 賣超分點" result={sellResult} scope={effectiveScope} names={names} onBranch={setBranch}/></div>
    <div className={styles.counts}><Card label="最新觀察日：有買進減有賣出家數" value={activeSeries.at(-1)?.value} unit="分點" status={activeRevisionMismatch ? "incompatible_data" : metricsEnabled ? activeCounts.data?.result.status : undefined}/>
      <Card label="最新觀察日：淨買超減淨賣超家數" value={netSeries.at(-1)?.value} unit="分點" status={netRevisionMismatch ? "incompatible_data" : metricsEnabled ? netCounts.data?.result.status : undefined}/></div>
    <p className={styles.note}>同一分點可同時買進與賣出；活躍家數差與淨方向家數差不是同一公式，不互相替代，也不加總每日家數當期間不同家數。</p>
    <div className={styles.branch}><label>指定分點<select aria-label="指定分點" value={branch} onChange={event => setBranch(event.target.value)}><option value="">請選擇分點</option>{[...names].map(([identifier, name]) => <option key={identifier} value={identifier}>{name}</option>)}</select></label>
      <Card label={period === "custom" ? "指定分點區間累計買賣超" : `指定分點 ${period} 交易日累計買賣超`} value={metricsEnabled && branch && !branchRevisionMismatch ? metricValue(branchMetric.data?.result) : null} unit={quantityUnit(branchMetric.data?.result.unit ?? last?.quantity_unit)} status={branchRevisionMismatch ? "incompatible_data" : metricsEnabled && branch ? branchMetric.data?.result.status : undefined}><Formula result={branchMetric.data?.result}/></Card>
      {metricsEnabled && branch && <ResourceState state={branchMetric} label="指定分點累計"/>}</div>
    {metricsEnabled && <div className={styles.charts}><div><ResourceState state={dailyProxy} label="每日分點 Proxy"/><ChipChart chartId={`${id}-broker-daily-proxy`} title="每日重選 TOP 15 分點 Proxy" unit={unit} series={flowSeries} frequency="1d" syncGroup={syncGroup}/></div>
      <div><ResourceState state={activeCounts} label="買賣家數差"/><ChipChart chartId={`${id}-broker-count-difference`} title="分點買賣家數差" unit="分點" frequency="1d" syncGroup={syncGroup} series={[
        { id: "active", label: "有買進減有賣出", color: "#ef8190", kind: "histogram", points: activeSeries },
        { id: "net", label: "淨買超減淨賣超", color: "#b4a4f5", kind: "line", points: netSeries },
      ]}/></div></div>}
    <p className={styles.note}>分點數量、家數與股價使用獨立圖表／單位；正負柱狀圖分色。缺值不補零；未取得核實區間時不畫完整主力柱狀圖。</p>
  </div>;
}
