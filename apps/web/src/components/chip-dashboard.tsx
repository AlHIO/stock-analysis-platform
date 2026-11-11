"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, getBars } from "../lib/market-api";
import { getInstitutionalHistory, getInstitutionalMetrics, getOwnershipHistory, getOwnershipMetrics,
  getMarginHistory, getLendingHistory } from "../lib/chip-api";
import type { ChipCalculation, ChipCoverage, ChipFreshness, ChipTiming, ChipProvenance,
  ChipQuality, ChipRange, InstitutionalCategory, LendingKind } from "../lib/chip-types";
import type { ChipSeries } from "../lib/chip-chart";
import { alignDailySeries, calculationValue, chipRange, chipStatus, quantityValue, validChipRange } from "../lib/chip-display";
import { decimal, dateLabel, timestamp } from "../lib/format";
import { ChipChart } from "./chip-chart";
import styles from "./chip-dashboard.module.css";

type Resource<T> = { key: string; revision: number; status: "loading"|"ready"|"error"; data?: T; error?: unknown };

/** Keep a visited panel and its charts mounted; abort superseded requests only. */
function useResource<T>(key: string, active: boolean, loader: (signal: AbortSignal) => Promise<T>) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<Resource<T>>({ key: "", revision: 0, status: "loading" });
  const current = useRef(state); current.current = state;
  const load = useRef(loader); load.current = loader;
  useEffect(() => {
    if (!active || (current.current.key === key && current.current.revision === revision && current.current.status !== "loading")) return;
    const controller = new AbortController();
    setState(previous => ({ key, revision, status: "loading", data: previous.data }));
    load.current(controller.signal).then(data => {
      if (!controller.signal.aborted) setState({ key, revision, status: "ready", data });
    }).catch(error => {
      if (!controller.signal.aborted) setState(previous => ({key,revision,status:"error",error,data:previous.data}));
    });
    return () => controller.abort();
  }, [key, active, revision]);
  return { ...state, retry: () => setRevision(n => n + 1) };
}

function LoadingError({ state }: { state: {status:string; data?:unknown; error?:unknown; retry:()=>void} }) {
  return <>
    {state.status === "loading" && <p className={styles.loading} role="status">正在載入籌碼資料…{state.data ? " 以下暫保留上一個成功結果。" : ""}</p>}
    {state.status === "error" && <div className={styles.warning} role="alert"><strong>{state.error instanceof ApiError && state.error.status < 500 ? "籌碼查詢條件或資料格式有誤" : "籌碼 API 暫時無法連線"}</strong><p>{state.error instanceof ApiError ? state.error.message : "請稍後重試。"}{state.data ? " 以下為先前成功讀取的資料，並非這次查詢結果。" : ""}</p><button onClick={state.retry}>重新載入籌碼</button></div>}
  </>;
}

type MetaPage = {coverage:ChipCoverage; freshness:ChipFreshness; read_at:string; next_cursor?:string|null; from:string; to:string};
type ObservedPoint = {timing:ChipTiming; provenance:ChipProvenance; quality:ChipQuality};
function DataStatus({ data, points, lastDate, frequency }: {data:MetaPage; points:readonly ObservedPoint[]; lastDate?:string; frequency:"1d"|"1w"}) {
  const last = points.at(-1);
  const sources = [...new Map(points.map(p => [p.provenance.source.id,p.provenance.source])).values()];
  const partial = data.coverage.completeness === "partial" || points.some(p => p.quality.status === "partial");
  const unverified = data.coverage.completeness === "not_verified" || points.some(p => p.quality.status === "not_verified");
  const stale = data.freshness.assessment === "stale" || data.freshness.assessment === "delayed";
  return <aside className={styles.status} aria-label="籌碼資料狀態">
    <div className={styles.badges}>
      <span>{frequency === "1w" ? "每週統計 · 未日化" : "每日交易統計"}</span>
      {partial && <span className={styles.amber}>Partial · 來源欄位不完整</span>}
      {unverified && <span className={styles.amber}>完整度尚未核實</span>}
      {stale ? <span className={styles.amber}>Stale · 來源已標示延遲</span> : <span>{data.freshness.assessment==="within_expected_window"?"來源預期更新範圍內":"時效未核實"}</span>}
      {(data.next_cursor || data.coverage.truncated) && <span className={styles.amber}>結果有截斷 · 請縮短日期區間</span>}
    </div>
    <dl className={styles.metadata}>
      <div><dt>最新統計日期</dt><dd>{dateLabel(lastDate)}</dd></div>
      <div><dt>官方發布時間</dt><dd>{last?.timing.source_published_at ? timestamp(last.timing.source_published_at) : last?.timing.source_published_on ? `${dateLabel(last.timing.source_published_on)}（僅日期，時刻未知）` : "未知，未自行推算"}</dd></div>
      <div><dt>首次觀察／匯入時間</dt><dd>{timestamp(last?.timing.first_seen_at)} / {timestamp(last?.timing.ingested_at)}</dd></div>
      <div><dt>資料來源</dt><dd>{sources.length ? sources.map(s=>`${s.name} (${s.code})`).join("、") : "尚無 Canonical 來源"}</dd></div>
    </dl>
    <p>查詢區間 {dateLabel(data.from)} — {dateLabel(data.to)} · 已保存資料，非即時行情。日期較舊不等於來源遲延；未核實發布日曆時不判定 Stale。</p>
    {last && <details><summary>來源版本與 Revision</summary><dl className={styles.metadata}>
      <div><dt>Revision</dt><dd>{last.provenance.revision_id}</dd></div>
      <div><dt>來源契約</dt><dd>{last.provenance.source_contract_version}</dd></div>
      <div><dt>Raw／Run</dt><dd>{last.provenance.raw_id} / {last.provenance.run_id}</dd></div>
    </dl></details>}
  </aside>;
}

function Empty({ children }: { children: ReactNode }) {
  return <div className={styles.empty} role="status"><span>Unavailable</span><h3>{children}</h3><p>資料庫沒有可顯示的對應報表；缺失不補零，也不產生假圖表。</p></div>;
}
function Metric({ label, value, unit, calculation }: { label:string; value?:string|null; unit:string; calculation?:ChipCalculation|null }) {
  const shown = calculation ? calculationValue(calculation) : value;
  return <div className={styles.metric}><span>{label}</span><strong>{decimal(shown)} <small>{shown != null ? unit : ""}</small></strong>
    <small>{calculation ? chipStatus(calculation.status) : shown == null ? "Unavailable" : "來源報表值"}</small>
    {calculation && <details><summary>計算依據</summary><p>{calculation.formula}</p><p>{calculation.algorithm_version}</p><p>{calculation.reasons.join(" · ") || "使用後端計算結果，前端不重算公式。"}</p></details>}
  </div>;
}
const CATEGORIES: {id:InstitutionalCategory;label:string;color:string}[] = [
  {id:"foreign_ex_dealers",label:"外資（不含外資自營商）",color:"#64d5c3"},
  {id:"investment_trust",label:"投信",color:"#b4a4f5"},
  {id:"dealer_total",label:"自營商合計",color:"#e6b66d"},
];
type PanelProps = {id:string;range:ChipRange;active:boolean;currency:string};

function InstitutionalPanel({id,range,active,currency}:PanelProps) {
  const [category,setCategory] = useState<InstitutionalCategory>("foreign_ex_dealers");
  const key=JSON.stringify([id,range]);
  const history=useResource(key,active,signal=>getInstitutionalHistory(id,range,signal));
  const metrics=useResource(`${key}/${category}`,active,signal=>getInstitutionalMetrics(id,category,range,signal));
  const metricData=metrics.data?.category===category?metrics.data:undefined;
  const price=useResource(key,active,signal=>getBars(id,"1d",signal,range));
  const rows=history.data?.items??[];
  const flows:ChipSeries[]=CATEGORIES.map(c=>({id:c.id,label:c.label,color:c.color,kind:"line",points:rows.map(r=>{
    const calculated=r.derived_daily.find(v=>v.category===c.id);
    return {date:r.trade_date,value:calculationValue(calculated),status:calculated?.status};
  })}));
  const priceSeries:ChipSeries={id:"close",label:"未還原日收盤價",color:"#a8c4ee",kind:"line",points:(price.data?.bars??[]).map(r=>({date:r.trade_date,value:r.close}))};
  const aligned=useMemo(()=>alignDailySeries([...flows,priceSeries]),[history.data,price.data]);
  const last=rows.at(-1);
  const unit=last?.quantity_unit==="units"?"單位":"股";
  const streak=metricData?.streak;
  return <div className={styles.panelBody}>
    <div className={styles.panelHeader}><div><h3>三大法人分析</h3><p>數量沿用來源核實單位；自營商自行買賣與避險由後端只合計一次。</p></div><label>法人分類<select value={category} onChange={e=>setCategory(e.target.value as InstitutionalCategory)}>{CATEGORIES.map(c=><option key={c.id} value={c.id}>{c.label}</option>)}</select></label></div>
    <LoadingError state={history}/><LoadingError state={metrics}/>
    {history.data && <DataStatus data={history.data} points={rows} lastDate={last?.trade_date} frequency="1d"/>}
    <div className={styles.cards}>{[5,10,20,60].map(period=>{
      const calculation=metricData?.cumulative.find(m=>m.period===period);
      return <Metric key={period} label={`${period} 交易日累計`} calculation={calculation} unit={calculation?.quantity_unit==="units"?"單位":calculation?.quantity_unit==="shares"?"股":unit}/>;
    })}</div>
    <div className={styles.streak}><strong>連買 {streak?.status==="complete" ? streak.buy_count??"—" : "—"} 個交易日 · 連賣 {streak?.status==="complete" ? streak.sell_count??"—" : "—"} 個交易日</strong><span>{chipStatus(streak?.status)}</span>
      {streak && streak.status!=="complete" && <p>已確認下限：買超 {streak.buy_lower_bound} 日／賣超 {streak.sell_lower_bound} 日；不視為完整連續次數。</p>}
    </div>
    {!rows.length && history.status==="ready" && <Empty>尚無已收錄三大法人資料</Empty>}
    {rows.length>0 && <div className={styles.chartGrid}>
      <div><ChipChart chartId={`${id}-institutional-flow`} title="法人每日買賣超" unit={unit} series={aligned.slice(0,3)} frequency="1d"/></div>
      <div><LoadingError state={price}/><ChipChart chartId={`${id}-institutional-price`} title="區間收盤價" unit={currency} series={aligned.slice(3)} frequency="1d"/></div>
    </div>}
    <p className={styles.note}>股價與法人數量分開座標軸，依同一查詢區間的實際日期對照；缺值留白。外資／投信／自營商口徑或完整交易日曆不足時，累計與連續統計顯示 Unavailable。</p>
  </div>;
}

function OwnershipPanel({id,range,active}:PanelProps) {
  const [threshold,setThreshold]=useState(400001);
  const [distributionDate,setDistributionDate]=useState("");
  const key=JSON.stringify([id,range]);
  const history=useResource(key,active,signal=>getOwnershipHistory(id,range,signal));
  const metrics=useResource(`${key}/${threshold}`,active,signal=>getOwnershipMetrics(id,threshold,range,signal));
  const metricData=metrics.data?.threshold===String(threshold)?metrics.data:undefined;
  const rows=history.data?.items??[];const last=rows.at(-1);const latestMetric=metricData?.items.at(-1);
  const distribution=rows.find(row=>row.as_of_date===distributionDate)??last;
  const ratio:ChipSeries={id:"ratio",label:"大戶持股比例",color:"#67d5c0",kind:"line",points:(metricData?.items??[]).map(r=>({date:r.as_of_date,value:calculationValue(r.ratio),status:r.ratio.status}))};
  const change:ChipSeries={id:"change",label:"週變化（百分點）",color:"#b4a4f5",kind:"histogram",points:(metricData?.items??[]).map(r=>({date:r.as_of_date,value:calculationValue(r.change_pp),status:r.change_pp.status}))};
  return <div className={styles.panelBody}>
    <div className={styles.panelHeader}><div><h3>集保股權分散</h3><p>僅依真正每週統計日顯示，不建立每日持股觀察。</p></div><label>大戶門檻<select value={threshold} onChange={e=>setThreshold(Number(e.target.value))}><option value={400001}>超過 400 張（400,001 股起）</option><option value={1000001}>超過 1,000 張（1,000,001 股起）</option></select></label></div>
    <LoadingError state={history}/><LoadingError state={metrics}/>
    {history.data && <DataStatus data={history.data} points={rows} lastDate={last?.as_of_date} frequency="1w"/>}
    <div className={styles.cards}><Metric label="大戶持股比例" calculation={latestMetric?.ratio} unit="%"/><Metric label="持股比例週變化" calculation={latestMetric?.change_pp} unit="百分點"/><Metric label="官方集保存量" value={last?.source_total_quantity} unit={last?.quantity_unit==="units"?"單位":"股"}/><Metric label="持有人數" value={last?.source_total_holders} unit="人"/></div>
    {!rows.length && history.status==="ready" && <Empty>尚無已收錄集保週資料</Empty>}
    {rows.length>0 && <>
      <div className={styles.chartGrid}><ChipChart chartId={`${id}-ownership-ratio-${threshold}`} title="大戶持股比例" unit="%" series={[ratio]} frequency="1w"/><ChipChart chartId={`${id}-ownership-change-${threshold}`} title="持股比例週變化" unit="百分點" series={[change]} frequency="1w"/></div>
      <label className={styles.channel}>級距分布統計日<select value={distribution?.as_of_date??""} onChange={e=>setDistributionDate(e.target.value)}>{rows.map(row=><option key={row.as_of_date} value={row.as_of_date}>{dateLabel(row.as_of_date)}</option>)}</select></label>
      <div className={styles.tableWrap}><table><caption>{dateLabel(distribution?.as_of_date)} 持股級距分布 · {distribution?.buckets.length} 個來源級距</caption><thead><tr><th>級距／類型</th><th>持有人數</th><th>持有數量（{distribution?.quantity_unit==="units"?"單位":"股"}）</th><th>來源持股比例</th></tr></thead><tbody>{distribution?.buckets.map(bucket=><tr key={bucket.code} data-kind={bucket.kind}><th>{bucket.label}<small>{bucket.kind==="regular"?"一般級距":bucket.kind==="adjustment"?"差異調整（獨立保存）":"官方合計"}</small></th><td>{decimal(bucket.holder_count)}</td><td>{decimal(bucket.quantity)}</td><td>{decimal(bucket.reported_percent)}{bucket.reported_percent!=null?"%":""}</td></tr>)}</tbody></table></div>
      {distribution && <details className={styles.status}><summary>所選週報來源與版本</summary><dl className={styles.metadata}>
        <div><dt>Source／級距版本</dt><dd>{distribution.provenance.source.name} / {distribution.bucket_scheme_version}</dd></div>
        <div><dt>Revision</dt><dd>{distribution.provenance.revision_id}</dd></div>
        <div><dt>官方發布時間</dt><dd>{distribution.timing.source_published_at?timestamp(distribution.timing.source_published_at):distribution.timing.source_published_on?dateLabel(distribution.timing.source_published_on):"未知，未自行推算"}</dd></div>
        <div><dt>匯入時間</dt><dd>{timestamp(distribution.timing.ingested_at)}</dd></div>
      </dl></details>}
    </>}
    <p className={styles.note}>大戶比例的分母是同份週報的官方集保存量；級距比例直接顯示來源值。差異調整、合計不重複加總。週變化單位是百分點，不是百分比報酬；未核實週曆或缺週時不計算，也不能據此識別特定個人交易。</p>
  </div>;
}

function MarginPanel({id,range,active}:PanelProps) {
  const state=useResource(JSON.stringify([id,range]),active,signal=>getMarginHistory(id,range,signal));
  const rows=state.data?.items??[];const last=rows.at(-1);const unit=last?.quantity_unit==="units"?"單位":"股";
  const series=(key:"financing"|"short_margin",change=false):ChipSeries=>({id:key,label:key==="financing"?"融資":"融券",color:key==="financing"?"#68d3c2":"#b4a4f5",kind:"line",points:rows.map(r=>({date:r.trade_date,value:change?calculationValue(r[key].daily_change):quantityValue(r[key].balance)}))});
  return <div className={styles.panelBody}><div className={styles.panelHeader}><div><h3>融資融券</h3><p>分別顯示兩個 Ledger 的存量與餘額增減；不把餘額當每日成交量。</p></div></div>
    <LoadingError state={state}/>{state.data && <DataStatus data={state.data} points={rows} lastDate={last?.trade_date} frequency="1d"/>}
    <div className={styles.cards}><Metric label="融資餘額" value={quantityValue(last?.financing.balance)} unit={unit}/><Metric label="融資餘額日增減" calculation={last?.financing.daily_change} unit={unit}/><Metric label="融券餘額" value={quantityValue(last?.short_margin.balance)} unit={unit}/><Metric label="融券餘額日增減" calculation={last?.short_margin.daily_change} unit={unit}/></div>
    {!rows.length && state.status==="ready" && <Empty>尚無已收錄融資融券資料</Empty>}
    {rows.length>0 && <><div className={styles.chartGrid}><ChipChart chartId={`${id}-financing`} title="融資餘額" unit={unit} series={[series("financing")]} frequency="1d"/><ChipChart chartId={`${id}-short-margin`} title="融券餘額" unit={unit} series={[series("short_margin")]} frequency="1d"/></div><ChipChart chartId={`${id}-margin-change`} title="餘額日增減" unit={unit} series={[series("financing",true),series("short_margin",true)]} frequency="1d"/></>}
    <p className={styles.note}>每日增減使用後端的「當日餘額 − 官方前日餘額」結果，不假設前一曆日有交易。數量由來源經核實後正規化，前端不自行乘以 1,000；融券不包含借券賣出。</p>
  </div>;
}

function LendingPanel({id,range,active}:PanelProps) {
  const [kind,setKind]=useState<LendingKind>("lending_short_sales");
  const [channel,setChannel]=useState("");
  const state=useResource(JSON.stringify([id,range,kind]),active,signal=>getLendingHistory(id,kind,range,signal));
  const all=state.data?.items.filter(r=>r.report_kind===kind)??[];
  const channels=[...new Map(all.map(r=>[`${r.lending_channel.family}/${r.lending_channel.code}/${r.lending_channel.version}`,r.lending_channel])).entries()];
  const selected=channels.length===1?channels[0][0]:channel;
  const rows=all.filter(r=>`${r.lending_channel.family}/${r.lending_channel.code}/${r.lending_channel.version}`===selected);
  const last=rows.at(-1);const short=kind==="lending_short_sales";const unit=last?.quantity_unit==="units"?"單位":"股";
  const flowKeys=short?["short_sales","short_returns"] as const:["borrowed_quantity","returned_quantity"] as const;
  const flows:ChipSeries[]=flowKeys.map((field,index)=>({id:field,label:short?(index===0?"當日借券賣出":"當日還券"):(index===0?"當日借入":"當日還券"),color:index===0?"#67d5c0":"#e0b56c",kind:"line",points:rows.map(r=>({date:r.trade_date,value:quantityValue(r.ledger[field])}))}));
  return <div className={styles.panelBody}><div className={styles.panelHeader}><div><h3>借券與借券賣出</h3><p>借券未還、借券賣出餘額與每日賣出量分開，不合計不同 Channel。</p></div><label>借券報表類型<select value={kind} onChange={e=>{setKind(e.target.value as LendingKind);setChannel("");}}><option value="lending_short_sales">借券賣出（餘額／日量）</option><option value="borrowed_positions">借券未還（借入／還券）</option></select></label></div>
    <LoadingError state={state}/>
    {channels.length>1 && <label className={styles.channel}>來源 Channel<select value={selected} onChange={e=>setChannel(e.target.value)}><option value="">請選擇單一 Channel</option>{channels.map(([key,v])=><option key={key} value={key}>{v.family} · {v.code}</option>)}</select></label>}
    {state.data && <DataStatus data={state.data} points={rows} lastDate={last?.trade_date} frequency="1d"/>}
    {last && <div className={styles.scope}><strong>來源範圍：{last.trade_scope==="unspecified"?"未核實（unspecified）":last.trade_scope}</strong><p>Channel：{last.lending_channel.family} / {last.lending_channel.code} · {last.lending_channel.verified?"已核實":"來源範圍未核實"}</p><p>以下為單一來源報表值；不因公式可核對就宣稱已確認所有管道，亦不計算未知分母的跨來源比例。</p></div>}
    <div className={styles.cards}><Metric label={short?"借券賣出餘額":"借券未還餘額"} value={quantityValue(last?.ledger.balance)} unit={unit}/><Metric label={short?"當日借券賣出":"當日新增借券"} value={quantityValue(short?last?.ledger.short_sales:last?.ledger.borrowed_quantity)} unit={unit}/><Metric label="當日還券" value={quantityValue(short?last?.ledger.short_returns:last?.ledger.returned_quantity)} unit={unit}/><Metric label="來源調整數" value={quantityValue(last?.ledger.signed_adjustment)} unit={unit}/></div>
    {!all.length && state.status==="ready" && <Empty>{short?"尚無已收錄借券賣出資料":"尚無可用的借券未還報表"}</Empty>}
    {rows.length>0 && <div className={styles.chartGrid}><ChipChart chartId={`${id}-${kind}-balance-${selected}`} title={short?"借券賣出餘額":"借券未還餘額"} unit={unit} series={[{id:"balance",label:"來源報表餘額",color:"#68d3c2",kind:"line",points:rows.map(r=>({date:r.trade_date,value:quantityValue(r.ledger.balance)}))}]} frequency="1d"/><ChipChart chartId={`${id}-${kind}-flow-${selected}`} title={short?"當日借券賣出與還券":"當日借入與還券"} unit={unit} series={flows} frequency="1d"/></div>}
    <p className={styles.note}>來源未支援或資料庫尚無對應報表時顯示 Unavailable；不拿借券賣出替代借券未還。Quota 是監管配額，不代表實際借入、賣出或餘額。調整數保留原始符號，未知發布時間不推估。</p>
  </div>;
}

const TABS=[{id:"institutional",label:"三大法人",view:InstitutionalPanel},{id:"ownership",label:"集保持股",view:OwnershipPanel},{id:"margin",label:"融資融券",view:MarginPanel},{id:"lending",label:"借券賣出",view:LendingPanel}] as const;
type Tab = typeof TABS[number]["id"];

export function ChipDashboard({id,marketDate,currency}:{id:string;marketDate:string;currency:string}) {
  const [active,setActive]=useState<Tab>("institutional");
  const [visited,setVisited]=useState<Set<Tab>>(()=>new Set(["institutional"]));
  const [range,setRange]=useState<ChipRange>(()=>chipRange(marketDate,90));
  const [draft,setDraft]=useState(range);const[error,setError]=useState("");
  function choose(tab:Tab){setActive(tab);setVisited(previous=>new Set([...previous,tab]));}
  function keyboard(event:KeyboardEvent<HTMLButtonElement>,index:number){
    const next=event.key==="ArrowRight"?(index+1)%TABS.length:event.key==="ArrowLeft"?(index+TABS.length-1)%TABS.length:event.key==="Home"?0:event.key==="End"?TABS.length-1:-1;
    if(next<0)return;event.preventDefault();choose(TABS[next].id);document.getElementById(`chip-tab-${TABS[next].id}`)?.focus();
  }
  function apply(event:FormEvent){event.preventDefault();const problem=validChipRange(draft,marketDate);setError(problem??"");if(!problem)setRange({...draft});}
  return <section className={styles.dashboard} aria-label="籌碼分析 Dashboard">
    <div className={styles.header}><div><span className={styles.eyebrow}>CHIP ANALYSIS</span><h2>籌碼分析</h2><p>依來源、日期與口徑，理解已收錄的持股與交易資料。</p></div><a href="#stock-price-chart">對照價格與技術指標 ↑</a></div>
    <div className={styles.range}>
      <div role="group" aria-label="籌碼時間區間" className={styles.presets}>{[{days:30,label:"近1個月"},{days:90,label:"近3個月"},{days:365,label:"近1年"}].map(v=><button key={v.days} aria-pressed={range.from===chipRange(range.to,v.days).from} onClick={()=>{const next=chipRange(marketDate,v.days);setRange(next);setDraft(next);setError("");}}>{v.label}</button>)}</div>
      <form onSubmit={apply} noValidate><label>籌碼開始日期<input type="date" value={draft.from} onChange={e=>setDraft(v=>({...v,from:e.target.value}))}/></label><label>籌碼結束日期<input type="date" value={draft.to} max={marketDate} onChange={e=>setDraft(v=>({...v,to:e.target.value}))}/></label><button type="submit">套用籌碼日期</button></form>
      {error && <p role="alert" className={styles.amber}>{error}</p>}
    </div>
    <div className={styles.tabs} role="tablist" aria-label="籌碼分析">{TABS.map((tab,index)=><button key={tab.id} id={`chip-tab-${tab.id}`} role="tab" aria-selected={active===tab.id} aria-controls={`chip-panel-${tab.id}`} tabIndex={active===tab.id?0:-1} onClick={()=>choose(tab.id)} onKeyDown={e=>keyboard(e,index)}>{tab.label}</button>)}</div>
    {TABS.map(tab=>{const View=tab.view;return <div key={tab.id} role="tabpanel" id={`chip-panel-${tab.id}`} aria-labelledby={`chip-tab-${tab.id}`} hidden={active!==tab.id}>{visited.has(tab.id)&&<View id={id} range={range} active={active===tab.id} currency={currency}/>}</div>;})}
  </section>;
}
