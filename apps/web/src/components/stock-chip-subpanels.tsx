"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/market-api";
import { getInstitutionalHistory, getOwnershipMetrics, getMarginHistory, getLendingHistory } from "../lib/chip-api";
import { getBrokerHistory, getBrokerMetrics } from "../lib/broker-api";
import { brokerDailyPoints } from "../lib/broker-types";
import type { ChipRange, InstitutionalCategory } from "../lib/chip-types";
import { calculationValue, chipStatus, quantityValue, validChipRange } from "../lib/chip-display";
import { timestamp } from "../lib/format";
import { ChipChart, type ChipSeries } from "./chip-chart";
import styles from "./stock-chip-subpanels.module.css";

type Kind = "institutional" | "broker" | "counts" | "ownership" | "margin" | "lending";
const PANES: {id:Kind;label:string}[] = [
  {id:"institutional",label:"法人買賣超"},{id:"broker",label:"分點 TOP N Proxy"},
  {id:"counts",label:"分點買賣家數差"},{id:"ownership",label:"集保大戶比例（週）"},
  {id:"margin",label:"融資／融券餘額"},{id:"lending",label:"借券賣出日量"},
];
type PaneData = {series:ChipSeries[];unit:string;frequency:"1d"|"1w";sources:string[];lastDate?:string;
  ingestedAt?:string|null;publishedAt?:string|null;notes:string[]};

function ObservedPane({id,kind,range,enabled,syncGroup}:{id:string;kind:Kind;range:ChipRange;enabled:boolean;syncGroup:string}) {
  const [category,setCategory] = useState<InstitutionalCategory>("foreign_ex_dealers");
  const [threshold,setThreshold] = useState(400001);
  const [ledger,setLedger] = useState<"financing"|"short_margin">("financing");
  const [scope,setScope] = useState("");
  const [topN,setTopN] = useState<5|10|15|30>(15);
  const [countKind,setCountKind] = useState<"active"|"net">("active");
  const [channel,setChannel] = useState("");
  const [scopes,setScopes] = useState<{id:string;source:string;label:string}[]>([]);
  const [channels,setChannels] = useState<string[]>([]);
  const [retry,setRetry] = useState(0);
  const key=JSON.stringify([id,kind,range,category,threshold,ledger,scope,topN,countKind,channel,retry]);
  const [state,setState]=useState<{key:string;status:"loading"|"ready"|"error";data?:PaneData;error?:unknown}>({key:"",status:"loading"});
  const current=useRef(state); current.current=state;
  useEffect(()=>{
    if(!enabled || current.current.key===key && current.current.status!=="loading") return;
    const abort=new AbortController();
    setState({key,status:"loading"});
    const line=(label:string,points:ChipSeries["points"],unit:string,frequency:"1d"|"1w",extra:Partial<PaneData>={}):PaneData=>({
      series:[{id:kind,label,color:kind==="ownership"?"#c2a6ee":"#e89e90",kind:kind==="ownership"||kind==="margin"?"line":"histogram",points}],
      unit,frequency,sources:[],notes:[],...extra});
    async function load():Promise<PaneData> {
      if(kind==="institutional") {
        const data=await getInstitutionalHistory(id,range,abort.signal);
        const last=data.items.at(-1);
        return line("法人每日買賣超",data.items.map(row=>({date:row.trade_date,value:calculationValue(row.derived_daily.find(c=>c.category===category))})),last?.quantity_unit==="units"?"單位":"股","1d",{
          sources:[...new Set(data.items.map(row=>row.provenance.source.name))],lastDate:last?.trade_date,
          ingestedAt:last?.timing.ingested_at,publishedAt:last?.timing.source_published_at,
          notes:[...(data.next_cursor||data.coverage.truncated?["Partial · 回應有截斷，請縮短期間。"]:[]),...(data.items.some(row=>row.quality.status==="partial")?["Partial · 有來源欄位缺失；缺值留白。"]:[]),
            ...(data.freshness.assessment==="stale"||data.freshness.assessment==="delayed"?["Stale · 來源標示延遲。"]:[])]});
      }
      if(kind==="ownership") {
        const data=await getOwnershipMetrics(id,threshold,range,abort.signal);
        return line("集保大戶持股比例",data.items.map(row=>({date:row.as_of_date,value:calculationValue(row.ratio)})),"%","1w",{
          sources:[...new Set(data.items.flatMap(row=>(row.ratio.input_refs??[]).flatMap(ref=>{
            const source=ref.source_id??ref.lineage?.source_id;return source?[`Source ${source}`]:[];})))],
          lastDate:data.items.at(-1)?.as_of_date,notes:["僅原始週統計日；沒有每日新觀察，不插值。來源名稱及原始週報時間可在集保持股 Tab 核對。",
            ...(data.next_cursor||data.coverage.truncated?["Partial · 結果有截斷，請縮短期間。"]:[]),
            ...(data.items.some(row=>row.ratio.status!=="complete")?["部分比例不可取得："+chipStatus(data.items.find(row=>row.ratio.status!=="complete")?.ratio.status)]:[])]});
      }
      if(kind==="margin") {
        const data=await getMarginHistory(id,range,abort.signal); const last=data.items.at(-1);
        return line(ledger==="financing"?"融資餘額":"融券餘額",data.items.map(row=>({date:row.trade_date,value:quantityValue(row[ledger].balance)})),last?.quantity_unit==="units"?"單位":"股","1d",{
          sources:[...new Set(data.items.map(row=>row.provenance.source.name))],lastDate:last?.trade_date,
          ingestedAt:last?.timing.ingested_at,publishedAt:last?.timing.source_published_at,
          notes:["餘額為存量，不加總成期間流量。",...(data.next_cursor||data.coverage.truncated?["Partial · 結果有截斷。"]:[])]});
      }
      if(kind==="lending") {
        const data=await getLendingHistory(id,"lending_short_sales",range,abort.signal);
        const keys=[...new Set(data.items.map(row=>`${row.lending_channel.family}/${row.lending_channel.code}/${row.lending_channel.version}`))];
        if(!abort.signal.aborted) setChannels(keys);
        const chosen=keys.length===1?keys[0]:channel;
        const rows=data.items.filter(row=>`${row.lending_channel.family}/${row.lending_channel.code}/${row.lending_channel.version}`===chosen);
        const last=rows.at(-1);
        return line("當日借券賣出",rows.map(row=>({date:row.trade_date,value:quantityValue(row.ledger.short_sales)})),last?.quantity_unit==="units"?"單位":"股","1d",{
          sources:[...new Set(rows.map(row=>row.provenance.source.name))],lastDate:last?.trade_date,
          ingestedAt:last?.timing.ingested_at,publishedAt:last?.timing.source_published_at,
          notes:["借券賣出日量，不是借券未還餘額。",...(keys.length>1&&!chosen?["Unavailable · 請選擇單一 Channel，不加總不同管道。"]:[]),
            ...(last&&(!last.lending_channel.verified||last.trade_scope==="unspecified")?["來源 Channel／交易口徑尚未核實。"]:[]),
            ...(data.next_cursor||data.coverage.truncated?["Partial · 結果有截斷。"]:[])]});
      }
      const history=await getBrokerHistory(id,range,{},abort.signal);
      const choices=[...new Map(history.items.map(row=>[row.scope.id,{id:row.scope.id,source:row.lineage.source_id,label:`${row.lineage.source.name} · ${row.scope.code} ${row.scope.version}`}])).values()];
      if(!abort.signal.aborted) setScopes(choices);
      const chosen=choices.find(row=>row.id===scope)??(choices.length===1?choices[0]:undefined);
      if(!chosen) return line(kind==="broker"?"每日重選 TOP N Proxy":"分點家數差",[],"來源單位","1d",{
        notes:[history.items.length?"Unavailable · 請選擇單一來源與交易口徑。":"Unavailable · 尚無已收錄分點資料。"]});
      const selection={trade_scope:chosen.id,source:chosen.source};
      const algorithm=kind==="broker"?"proxy.broker_daily_top_n_imbalance_sum":countKind==="active"?"broker.active_buy_sell_count_difference":"broker.net_buy_sell_count_difference";
      const response=await getBrokerMetrics(id,algorithm,range,kind==="broker"?{top_n:topN,selection:"daily_reselect"}:{},selection,abort.signal);
      const result=response.result; const last=result.input_refs.at(-1);
      return line(kind==="broker"?`每日重選 TOP ${topN} 分點 Proxy`:countKind==="active"?"有買進減有賣出分點家數":"淨買超減淨賣超分點家數",
        brokerDailyPoints(result),kind==="counts"?"分點":result.unit==="units"?"單位":result.unit==="shares"?"股":"來源單位","1d",{
          sources:response.sources.map(source=>source.name),lastDate:last?.statistics_date,
          ingestedAt:last?.ingested_at,publishedAt:last?.source_published_at,
          notes:[chipStatus(result.status),"Proxy 不是官方主力或個別投資人流量；公開展示權尚未核實。",
            ...(history.next_cursor||history.coverage.truncated?["口徑選項來自有限報表頁；請縮短期間以檢查更多來源。"]:[])]});
    }
    load().then(data=>{if(!abort.signal.aborted)setState({key,status:"ready",data});}).catch(error=>{if(!abort.signal.aborted)setState({key,status:"error",error});});
    return()=>abort.abort();
  },[key,enabled,id,kind,range,category,threshold,ledger,scope,topN,countKind,channel]);
  const stateMatches=state.key===key;
  const data=stateMatches?state.data:undefined;
  return <section className={styles.pane} hidden={!enabled} aria-label={`${PANES.find(item=>item.id===kind)?.label}副圖`}>
    <div className={styles.options}>
      {kind==="institutional"&&<label>副圖法人<select value={category} onChange={event=>setCategory(event.target.value as InstitutionalCategory)}><option value="foreign_ex_dealers">外資（不含外資自營商）</option><option value="investment_trust">投信</option><option value="dealer_total">自營商合計</option></select></label>}
      {kind==="ownership"&&<label>副圖大戶門檻<select value={threshold} onChange={event=>setThreshold(Number(event.target.value))}><option value={400001}>400,001 股起</option><option value={1000001}>1,000,001 股起</option></select></label>}
      {kind==="margin"&&<label>副圖融資融券<select value={ledger} onChange={event=>setLedger(event.target.value as typeof ledger)}><option value="financing">融資餘額</option><option value="short_margin">融券餘額</option></select></label>}
      {(kind==="broker"||kind==="counts")&&scopes.length>1&&<label>副圖分點來源口徑<select value={scope} onChange={event=>setScope(event.target.value)}><option value="">請選擇</option>{scopes.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}
      {kind==="broker"&&<label>副圖 TOP N<select value={topN} onChange={event=>setTopN(Number(event.target.value) as typeof topN)}>{[5,10,15,30].map(n=><option key={n} value={n}>{n}</option>)}</select></label>}
      {kind==="counts"&&<label>副圖家數定義<select value={countKind} onChange={event=>setCountKind(event.target.value as typeof countKind)}><option value="active">有買進家數 − 有賣出家數</option><option value="net">淨買超家數 − 淨賣超家數</option></select></label>}
      {kind==="lending"&&channels.length>1&&<label>副圖借券 Channel<select value={channel} onChange={event=>setChannel(event.target.value)}><option value="">請選擇</option>{channels.map(value=><option key={value}>{value}</option>)}</select></label>}
    </div>
    {(!stateMatches||state.status==="loading")&&<p role="status">正在載入副圖…</p>}
    {stateMatches&&state.status==="error"&&<div role="alert"><p>{state.error instanceof ApiError&&state.error.status<500?"副圖查詢條件或資料格式有誤":"副圖 API 暫時無法連線"}</p><button onClick={()=>setRetry(value=>value+1)}>重試副圖</button></div>}
    {data&&<><ChipChart chartId={`${id}-subpane-${kind}-${category}-${threshold}-${ledger}-${scope}-${topN}-${countKind}-${channel}`} title={data.series[0].label} series={data.series} unit={data.unit} frequency={data.frequency} syncGroup={syncGroup}/>
      <p className={styles.meta}>統計日期：{data.lastDate??"尚無觀察"} · 來源：{data.sources.join("、")||"尚無可用來源"} · 發布：{timestamp(data.publishedAt)} · 匯入：{timestamp(data.ingestedAt)}</p>
      {data.notes.map(note=><p className={styles.meta} key={note}>{note}</p>)}</>}
  </section>;
}

export function StockChipSubpanels({id,range,syncGroup}:{id:string;range?:ChipRange;syncGroup:string}) {
  const [enabled,setEnabled]=useState<Partial<Record<Kind,boolean>>>({});
  const [visited,setVisited]=useState<Set<Kind>>(()=>new Set());
  const problem=range?validChipRange(range,range.to):"請先選擇可用的價格日期範圍。";
  return <section className={styles.workspace} aria-label="K 線籌碼副圖">
    <div className={styles.header}><h3>籌碼副圖</h3><span>共用實際日期十字線 · 各圖保留獨立單位</span></div>
    <div className={styles.toggles} role="group" aria-label="選擇籌碼副圖">{PANES.map(item=><label key={item.id}><input type="checkbox" checked={!!enabled[item.id]}
      onChange={event=>{const checked=event.target.checked;setEnabled(old=>({...old,[item.id]:checked}));if(checked)setVisited(old=>new Set([...old,item.id]));}}/>{item.label}</label>)}</div>
    <p className={styles.meta}>使用價格查詢範圍 {range?.from??"—"} — {range?.to??"—"}。日／週數據保持原始頻率，無觀察日期不補值；開關不重設價格圖。</p>
    {problem&&Object.values(enabled).some(Boolean)&&<p role="status" className={styles.meta}>Unavailable · {problem}</p>}
    {range&&!problem&&PANES.filter(item=>visited.has(item.id)).map(item=><ObservedPane key={item.id} id={id} kind={item.id} range={range} enabled={!!enabled[item.id]} syncGroup={syncGroup}/>)}
  </section>;
}
