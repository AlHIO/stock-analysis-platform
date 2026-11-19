/** Canonical Broker reads through the existing fixed-origin Next.js proxy. */
import { ApiError, request } from "./market-api";
import type { ChipRange } from "./chip-types";
import {
  BROKER_ALGORITHMS, brokerDate, brokerObject, brokerUuid,
  isBrokerHistoryResponse, isBrokerLatestResponse, isBrokerMetricsResponse, isBrokerStatusResponse,
  type BrokerAlgorithm, type BrokerHistoryOptions, type BrokerMetricResult, type BrokerSelection, type BrokerTopOptions,
} from "./broker-types";

const invalid=()=>new ApiError(502,"invalid_response","分點 API 回傳的日期、來源或計算參數與查詢不一致，無法安全顯示。");
const bad=()=>new ApiError(422,"invalid_parameters","分點查詢條件無效，請確認日期與來源選擇。");
const UUID_KEYS=["trade_scope","source","broker","branch"] as const;
function selectionQuery(selection:BrokerSelection):Record<string,string>{
  const values:Record<string,string>={};
  for(const key of UUID_KEYS){const value=selection[key];if(value!==undefined){if(!brokerUuid(value))throw bad();values[key]=value;}}
  return values;
}
function base(id:string,range?:ChipRange){
  if(!brokerUuid(id))throw bad();
  if(range&&(!brokerDate(range.from)||!brokerDate(range.to)||range.from>range.to))throw bad();
  return `stocks/${encodeURIComponent(id)}/broker-flow`;
}
function assertSelection(scopes:{id:string;source_id:string}[],selection:BrokerSelection){
  if(scopes.some(scope=>selection.trade_scope!==undefined&&scope.id!==selection.trade_scope||selection.source!==undefined&&scope.source_id!==selection.source))throw invalid();
}
function checkMetric(result:BrokerMetricResult,id:string,range:ChipRange,algorithm:BrokerAlgorithm,selection:BrokerSelection,params?:Record<string,unknown>,sessions?:number){
  if(result.request.instrument_id!==id||result.metric!==algorithm)throw invalid();
  if(sessions!==undefined){const window=result.details.session_window;
    if(!brokerObject(window)||window.requested_sessions!==sessions||window.query_from!==range.from||window.query_to!==range.to||typeof window.verified!=="boolean")throw invalid();
    const dates=window.expected_dates;
    if(!Array.isArray(dates)||dates.length>sessions||!dates.every(brokerDate)||new Set(dates).size!==dates.length||dates.some((date,index)=>date>range.to||index>0&&date<=String(dates[index-1])))throw invalid();
    const original=result.request.start===range.from&&result.request.end===range.to;
    const selected=window.verified&&dates.length===sessions&&dates[0]===result.request.start&&dates.at(-1)===result.request.end;
    if(!original&&!selected||result.status==="complete"&&!selected)throw invalid();
  }else if(result.request.start!==range.from||result.request.end!==range.to)throw invalid();
  if(selection.source&&result.input_refs.some(ref=>ref.product==="broker_flow"&&ref.source_id!==selection.source))throw invalid();
  if(selection.trade_scope&&result.input_refs.some(ref=>ref.product==="broker_flow"&&(!brokerObject(ref.metadata.scope)||ref.metadata.scope.id!==selection.trade_scope)))throw invalid();
  if(selection.branch&&result.parameters.branch_id!==selection.branch&&algorithm!=="broker.top_branches")throw invalid();
  if(params&&Object.entries(params).some(([key,value])=>JSON.stringify(result.parameters[key])!==JSON.stringify(value)))throw invalid();
}

export async function getBrokerHistory(id:string,range:ChipRange,options:BrokerHistoryOptions={},signal?:AbortSignal){
  const path=base(id,range);const {limit=50,order="asc",cursor}=options;
  if(!Number.isSafeInteger(limit)||limit<1||limit>250||!["asc","desc"].includes(order)||cursor!==undefined&&(typeof cursor!=="string"||cursor.length>2048))throw bad();
  const query=new URLSearchParams({from:range.from,to:range.to,limit:String(limit),order,...selectionQuery(options),...(cursor?{cursor}:{})});
  const result=await request(`${path}?${query}`,isBrokerHistoryResponse,signal);
  if(result.instrument.id!==id||result.from!==range.from||result.to!==range.to||result.limit!==limit)throw invalid();
  if(result.items.some((item,index)=>index>0&&((`${result.items[index-1].trade_date}/${result.items[index-1].scope.id}`<`${item.trade_date}/${item.scope.id}`)!==(order==="asc"))))throw invalid();
  assertSelection(result.items.map(item=>item.scope),options);
  if(options.branch&&result.items.some(item=>item.trades.some(trade=>trade.branch_id!==options.branch))||options.broker&&result.items.some(item=>item.trades.some(trade=>trade.identity.broker_id!==options.broker)))throw invalid();
  return result;
}
export async function getBrokerLatest(id:string,selection:BrokerSelection={},signal?:AbortSignal){
  const query=new URLSearchParams(selectionQuery(selection));
  const result=await request(`${base(id)}/latest${query.size?`?${query}`:""}`,isBrokerLatestResponse,signal);
  if(result.instrument.id!==id)throw invalid();
  if(result.point){assertSelection([result.point.scope],selection);if(selection.branch&&result.point.trades.some(t=>t.branch_id!==selection.branch)||selection.broker&&result.point.trades.some(t=>t.identity.broker_id!==selection.broker))throw invalid();}
  return result;
}
export async function getBrokerTopBranches(id:string,range:ChipRange,options:BrokerTopOptions={},signal?:AbortSignal){
  const path=base(id,range);const {top_n=15,side="net_buyers",sessions}=options;
  if(![5,10,15,30].includes(top_n)||!["net_buyers","net_sellers"].includes(side)||sessions!==undefined&&![1,5,10,20,60].includes(sessions))throw bad();
  const query=new URLSearchParams({from:range.from,to:range.to,top_n:String(top_n),side,...selectionQuery(options),...(sessions!==undefined?{sessions:String(sessions)}:{})});
  const result=await request(`${path}/top-branches?${query}`,isBrokerMetricsResponse,signal);
  checkMetric(result.result,id,range,"broker.top_branches",options,{top_n,side},sessions);
  return result;
}
export async function getBrokerMetrics(id:string,algorithm:BrokerAlgorithm,range:ChipRange,parameters:Record<string,unknown>={},selection:BrokerSelection={},signal?:AbortSignal){
  const path=base(id,range);
  if(!BROKER_ALGORITHMS.includes(algorithm)||!brokerObject(parameters))throw bad();
  let encoded:string;
  try{encoded=JSON.stringify(parameters,(_key,value:unknown)=>{if(value===undefined||typeof value==="function"||typeof value==="symbol"||typeof value==="number"&&!Number.isSafeInteger(value))throw bad();return value;});}catch{throw bad();}
  if(encoded.length>4096)throw bad();
  const query=new URLSearchParams({from:range.from,to:range.to,algorithm,algorithm_parameters:encoded,...selectionQuery(selection)});
  const result=await request(`${path}/metrics?${query}`,isBrokerMetricsResponse,signal);
  checkMetric(result.result,id,range,algorithm,selection,parameters);
  return result;
}
export async function getBrokerStatus(options:{source?:string;limit?:number}={},signal?:AbortSignal){
  const {limit=50,source}=options;
  if(!Number.isSafeInteger(limit)||limit<1||limit>100||source!==undefined&&!brokerUuid(source))throw bad();
  const query=new URLSearchParams({limit:String(limit),...(source?{source}:{})});
  const result=await request(`broker-flow/status?${query}`,isBrokerStatusResponse,signal);
  if(source&&result.items.some(item=>item.source.id!==source))throw invalid();
  return result;
}
