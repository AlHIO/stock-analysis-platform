/** H5 Canonical projections. Financial values remain exact API strings. */
import { isInstrument, type Instrument } from "./market-types";
import type { ChipDatum } from "./chip-chart";

export const BROKER_ALGORITHMS = [
  "broker.branch_net_quantity", "broker.branch_net_amount", "broker.branch_range_net_quantity",
  "broker.top_branches", "broker.branch_session_cumulative_net", "broker.branch_buy_streak", "broker.branch_sell_streak",
  "broker.active_buy_branch_count", "broker.active_sell_branch_count", "broker.active_buy_sell_count_difference",
  "broker.net_buy_branch_count", "broker.net_sell_branch_count", "broker.net_buy_sell_count_difference",
  "broker.period_active_branch_counts", "broker.period_net_branch_counts", "broker.gross_turnover_branch_share",
  "broker.top_n_gross_turnover_share", "broker.gross_turnover_hhi", "proxy.broker_top_n_net_imbalance",
  "proxy.broker_daily_top_n_imbalance_sum", "proxy.broker_daily_gross_amount_threshold_net",
  "broker.flow_price_alignment", "broker.source_coverage", "investor.medium_large_net_flow",
] as const;
export type BrokerAlgorithm = typeof BROKER_ALGORITHMS[number];
export type BrokerStatus = "complete"|"partial"|"missing_data"|"not_verified"|"insufficient_history"|"incompatible_data"|"truncated_history"|"no_sessions"|"unsupported"|"unavailable"|"invalid"|"not_yet_available";
export type BrokerJson = string | number | boolean | null | BrokerJson[] | {[key:string]:BrokerJson};
export type BrokerUnit = "shares"|"units";
export type BrokerSelection = {trade_scope?:string;source?:string;broker?:string;branch?:string};
export type BrokerHistoryOptions = BrokerSelection & {limit?:number;order?:"asc"|"desc";cursor?:string};
export type BrokerTopOptions = BrokerSelection & {top_n?:5|10|15|30;side?:"net_buyers"|"net_sellers";sessions?:1|5|10|20|60};
export type BrokerSource = {id:string;code:string;name:string};
export type BrokerScope = {id:string;source_id:string;market_id:string;code:string;version:string;session:string;participant_universe:string;correction_basis:string;description:string;odd_lot_included:boolean|null;block_trade_included:boolean|null;verified:boolean};
export type BrokerIdentity = {identity_id:string;broker_id:string;branch_id:string;broker_code:string;branch_code:string;branch_name:string;identity_version:string;valid_from:string;valid_to:string|null;verified:boolean};
export type BrokerTrade = {branch_id:string;identity_id:string;identity:BrokerIdentity;quantity_unit:BrokerUnit;buy_quantity:string|null;sell_quantity:string|null;reported_net_quantity:string|null;net_quantity:string|null;buy_amount:string|null;sell_amount:string|null;net_amount:string|null};
export type BrokerReport = {
  instrument_id:string;market_id:string;trade_date:string;scope:BrokerScope;quantity_unit:BrokerUnit;report_kind:string;
  price_basis:string;price_quantity_basis:string;currency:string|null;completeness:"complete"|"partial";
  branch_universe_complete:boolean;total_branch_count:number;filtered:boolean;trades:BrokerTrade[];
  observation_id:string;promotion_policy_version:string;date_evidence_reference:string;
  timing:{trade_date:string;source_published_at:string|null;source_generated_at:string|null;first_seen_at:string;ingested_at:string};
  lineage:{source:BrokerSource;revision_id:string;source_id:string;run_id:string;raw_payload_id:string;payload_sha256:string;source_namespace:string;source_contract_version:string;schema_version:string;parser_version:string;identity_policy_version:string};
  quality:{status:BrokerStatus;reasons:string[];provisional:boolean;point_in_time_supported:false};
  calculations:{algorithm_id:string;algorithm_version:"1.0.0";formula:string}[];
};
export type BrokerUsage = {automated_download_enabled:false;public_display_verified:false;derived_display_verified:false;assessment:"not_verified"};
export type BrokerCoverage = {stored_from:string|null;stored_to:string|null;stored_report_count:number;completeness:"not_verified";expected_dates_known:false;known_missing_dates:string[];truncated:boolean};
export type BrokerFreshness = {latest_trade_date:string|null;latest_source_published_at:string|null;first_seen_at:string|null;ingested_at:string|null;age_calendar_days:number|null;assessment:"no_data"|"not_verified";is_realtime:false};
type BrokerEnvelope = {instrument:Instrument;product:"broker_flow_daily";frequency:"1d";read_at:string;usage:BrokerUsage};
export type BrokerHistoryResponse = BrokerEnvelope & {from:string;to:string;items:BrokerReport[];count:number;limit:number;next_cursor:string|null;freshness:BrokerFreshness;coverage:BrokerCoverage};
export type BrokerLatestResponse = BrokerEnvelope & {point:BrokerReport|null;freshness:BrokerFreshness;coverage:BrokerCoverage};
export type BrokerInputReference = {product:string;instrument_id:string;market_id:string;statistics_date:string;revision_id:string;source_id:string;source_contract_version:string;first_seen_at:string;ingested_at:string;source_published_at:string|null;metadata:Record<string,BrokerJson>};
export type BrokerMetricResult = {
  request:{instrument_id:string;market_id:string;metric:string;start:string;end:string;query_as_of:string;parameters:Record<string,BrokerJson>;availability_policy:"observed_revision_v1"|"published_and_observed_revision_v1";timezone_name:string;engine_version:"broker-flow-engine-1.0.0"};
  metric:string;value:BrokerJson;status:BrokerStatus;formula:string|null;parameters:Record<string,BrokerJson>;unit:string|null;
  reasons:string[];expected_dates:string[];missing_dates:string[];input_refs:BrokerInputReference[];details:Record<string,BrokerJson>;
  selected_branch_ids:string[];provisional:boolean;point_in_time_supported:boolean;algorithm_version:"1.0.0"|null;engine_version:"broker-flow-engine-1.0.0";reproducibility_key:string;
};
export type BrokerMetricsResponse = BrokerEnvelope & {result:BrokerMetricResult;sources:BrokerSource[]};
export type BrokerMetricResponse = BrokerMetricsResponse;
export type BrokerTopBranchesResponse = BrokerMetricsResponse;
export type BrokerRun = {id:string;status:string;started_at:string;finished_at:string|null;counts:Record<string,number>;safe_failure_code:string|null};
export type BrokerSourceStatus = {source:BrokerSource;canonical_count:number;revision_count:number;raw_count:number;audit_counts:Record<string,number>;coverage:BrokerCoverage;latest_canonical_date:string|null;latest_source_published_at:string|null;last_seen_at:string|null;latest_run:BrokerRun|null;capabilities:{automated_ingestion_enabled:false;public_display_verified:false;derived_display_verified:false;license_status:"blocked"}};
export type BrokerStatusResponse = {product:"broker_flow_daily";frequency:"1d";items:BrokerSourceStatus[];count:number;read_at:string;usage:BrokerUsage;truncated:boolean};
export type BrokerRankRow = {branch_id:string;buy_quantity:string;sell_quantity:string;net_quantity:string;rank:number;identity_ids:string[];buy_amount?:string|null;sell_amount?:string|null};

const STATUSES = new Set(["complete","partial","missing_data","not_verified","insufficient_history","incompatible_data","truncated_history","no_sessions","unsupported","unavailable","invalid","not_yet_available"]);
export const brokerObject = (v:unknown):v is Record<string,unknown> => typeof v==="object"&&v!==null&&!Array.isArray(v);
export const brokerUuid = (v:unknown):v is string => typeof v==="string"&&/^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i.test(v);
export function brokerDate(v:unknown):v is string {if(typeof v!=="string"||!/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;const d=new Date(`${v}T00:00:00Z`);return Number.isFinite(d.getTime())&&d.toISOString().slice(0,10)===v;}
const clock=(v:unknown):v is string=>typeof v==="string"&&/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(v)&&Number.isFinite(Date.parse(v));
const nullable=<T>(v:unknown,guard:(x:unknown)=>x is T):v is T|null=>v===null||guard(v);
const text=(v:unknown):v is string=>typeof v==="string"&&v.length>0&&v.length<=4096;
const count=(v:unknown):v is number=>typeof v==="number"&&Number.isSafeInteger(v)&&v>=0;
const strings=(v:unknown):v is string[]=>Array.isArray(v)&&v.every(text);
const hash=(v:unknown):v is string=>typeof v==="string"&&/^[a-f0-9]{64}$/.test(v);
const unit=(v:unknown):v is BrokerUnit=>v==="shares"||v==="units";
export function brokerInteger(v:unknown):v is string {if(typeof v!=="string"||v.length>50||! /^-?(?:0|[1-9][0-9]*)$/.test(v)) return false;return true;}
const quantity=(v:unknown):v is string=>brokerInteger(v)&&v.length<=20&&BigInt(v)>=BigInt("-9223372036854775808")&&BigInt(v)<=BigInt("9223372036854775807");
const nonnegativeQuantity=(v:unknown):v is string=>quantity(v)&&!v.startsWith("-");
export const brokerDecimal=(v:unknown):v is string=>typeof v==="string"&&v.length<=80&&/^-?(?:0|[1-9][0-9]*)(?:\.\d{1,12})?$/.test(v)&&v.replace(/[-.]/g,"").length<=50;
const FINANCIAL_KEYS=new Set(["buy_quantity","sell_quantity","net_quantity","reported_net_quantity","sum_reported_daily_net_quantity","selected_buy_quantity","selected_sell_quantity","top_positive_net","top_negative_net_magnitude","proxy_net","buy_amount","sell_amount","net_amount","threshold_amount","price_close","close","denominator","numerator"]);
const QUANTITY_KEYS=new Set(["buy_quantity","sell_quantity","net_quantity","reported_net_quantity","sum_reported_daily_net_quantity","selected_buy_quantity","selected_sell_quantity","top_positive_net","top_negative_net_magnitude","proxy_net"]);
function exactJson(v:unknown,depth=0):v is BrokerJson {if(depth>20)return false;if(v===null||typeof v==="boolean"||typeof v==="string")return true;if(typeof v==="number")return Number.isSafeInteger(v);if(Array.isArray(v))return v.length<=10000&&v.every(x=>exactJson(x,depth+1));return brokerObject(v)&&Object.keys(v).length<=10000&&Object.entries(v).every(([key,x])=>(!FINANCIAL_KEYS.has(key)||x===null||(QUANTITY_KEYS.has(key)?brokerInteger(x):brokerDecimal(x)))&&exactJson(x,depth+1));}
const jsonRecord=(v:unknown):v is Record<string,BrokerJson>=>brokerObject(v)&&exactJson(v);
const usage=(v:unknown):v is BrokerUsage=>brokerObject(v)&&v.automated_download_enabled===false&&v.public_display_verified===false&&v.derived_display_verified===false&&v.assessment==="not_verified";
const source=(v:unknown):v is BrokerSource=>brokerObject(v)&&brokerUuid(v.id)&&text(v.code)&&text(v.name);
function scope(v:unknown):v is BrokerScope {return brokerObject(v)&&[v.id,v.source_id,v.market_id].every(brokerUuid)&&[v.code,v.version,v.session,v.participant_universe,v.correction_basis,v.description].every(text)&&[v.odd_lot_included,v.block_trade_included].every(x=>x===null||typeof x==="boolean")&&typeof v.verified==="boolean";}
function identity(v:unknown):v is BrokerIdentity{return brokerObject(v)&&[v.identity_id,v.broker_id,v.branch_id].every(brokerUuid)&&[v.broker_code,v.branch_code,v.branch_name,v.identity_version].every(text)&&brokerDate(v.valid_from)&&nullable(v.valid_to,brokerDate)&&(v.valid_to===null||v.valid_from<v.valid_to)&&typeof v.verified==="boolean";}
function trade(v:unknown):v is BrokerTrade {return brokerObject(v)&&brokerUuid(v.branch_id)&&brokerUuid(v.identity_id)&&identity(v.identity)&&v.identity.branch_id===v.branch_id&&v.identity.identity_id===v.identity_id&&unit(v.quantity_unit)&&[v.buy_quantity,v.sell_quantity].every(x=>nullable(x,nonnegativeQuantity))&&[v.net_quantity,v.reported_net_quantity].every(x=>nullable(x,quantity))&&[v.buy_amount,v.sell_amount].every(x=>x===null||brokerDecimal(x)&&!x.startsWith("-"))&&nullable(v.net_amount,brokerDecimal);}
function coverage(v:unknown):v is BrokerCoverage{return brokerObject(v)&&nullable(v.stored_from,brokerDate)&&nullable(v.stored_to,brokerDate)&&count(v.stored_report_count)&&v.completeness==="not_verified"&&v.expected_dates_known===false&&Array.isArray(v.known_missing_dates)&&v.known_missing_dates.every(brokerDate)&&typeof v.truncated==="boolean";}
function freshness(v:unknown):v is BrokerFreshness{return brokerObject(v)&&nullable(v.latest_trade_date,brokerDate)&&[v.latest_source_published_at,v.first_seen_at,v.ingested_at].every(x=>nullable(x,clock))&&nullable(v.age_calendar_days,count)&&["no_data","not_verified"].includes(String(v.assessment))&&v.is_realtime===false;}
function envelope(v:unknown):v is BrokerEnvelope & Record<string,unknown>{return brokerObject(v)&&isInstrument(v.instrument)&&v.product==="broker_flow_daily"&&v.frequency==="1d"&&clock(v.read_at)&&usage(v.usage);}
export function isBrokerReport(v:unknown):v is BrokerReport {
  if(!brokerObject(v)||![v.instrument_id,v.market_id,v.observation_id].every(brokerUuid)||!brokerDate(v.trade_date)||!scope(v.scope)||v.scope.market_id!==v.market_id||!unit(v.quantity_unit)||![v.report_kind,v.price_basis,v.price_quantity_basis,v.promotion_policy_version,v.date_evidence_reference].every(text)||!nullable(v.currency,text)||!["complete","partial"].includes(String(v.completeness))||typeof v.branch_universe_complete!=="boolean"||!count(v.total_branch_count)||typeof v.filtered!=="boolean"||!Array.isArray(v.trades)||v.trades.length>20000||!v.trades.every(trade))return false;
  if(v.trades.length>v.total_branch_count||(!v.filtered&&v.trades.length!==v.total_branch_count)||new Set(v.trades.map(t=>t.branch_id)).size!==v.trades.length||v.trades.some(t=>t.quantity_unit!==v.quantity_unit||t.identity.valid_from>String(v.trade_date)||(t.identity.valid_to!==null&&t.identity.valid_to<=String(v.trade_date))))return false;
  const t=v.timing,l=v.lineage,q=v.quality;
  return brokerObject(t)&&t.trade_date===v.trade_date&&[t.source_published_at,t.source_generated_at].every(x=>nullable(x,clock))&&clock(t.first_seen_at)&&clock(t.ingested_at)&&Date.parse(t.first_seen_at)<=Date.parse(t.ingested_at)&&brokerObject(l)&&source(l.source)&&[l.revision_id,l.source_id,l.run_id,l.raw_payload_id].every(brokerUuid)&&l.source.id===l.source_id&&l.source_id===v.scope.source_id&&hash(l.payload_sha256)&&[l.source_namespace,l.source_contract_version,l.schema_version,l.parser_version,l.identity_policy_version].every(text)&&brokerObject(q)&&STATUSES.has(String(q.status))&&strings(q.reasons)&&typeof q.provisional==="boolean"&&q.point_in_time_supported===false&&Array.isArray(v.calculations)&&v.calculations.every(x=>brokerObject(x)&&text(x.algorithm_id)&&x.algorithm_version==="1.0.0"&&text(x.formula));
}
export function isBrokerHistoryResponse(v:unknown):v is BrokerHistoryResponse {
  if(!envelope(v)||!brokerObject(v)||!brokerDate(v.from)||!brokerDate(v.to)||v.from>v.to||!Array.isArray(v.items)||!v.items.every(isBrokerReport)||!count(v.count)||v.count!==v.items.length||!count(v.limit)||v.limit<1||v.limit>250||v.count>v.limit||!nullable(v.next_cursor,text)||!freshness(v.freshness)||!coverage(v.coverage))return false;
  const keys=new Set<string>();let tradeCount=0;
  return v.items.every(r=>{const key=`${r.trade_date}/${r.scope.id}`;tradeCount+=r.trades.length;if(keys.has(key)||r.instrument_id!==v.instrument.id||r.market_id!==v.instrument.market.id||r.quantity_unit!==v.instrument.quantity_unit||r.trade_date<String(v.from)||r.trade_date>String(v.to)||tradeCount>20000)return false;keys.add(key);return true;});
}
export function isBrokerLatestResponse(v:unknown):v is BrokerLatestResponse{return envelope(v)&&brokerObject(v)&&freshness(v.freshness)&&coverage(v.coverage)&&(v.point===null||isBrokerReport(v.point)&&v.point.instrument_id===v.instrument.id&&v.point.market_id===v.instrument.market.id&&v.point.quantity_unit===v.instrument.quantity_unit);}
function inputReference(v:unknown):v is BrokerInputReference{return brokerObject(v)&&text(v.product)&&[v.instrument_id,v.market_id,v.revision_id,v.source_id].every(brokerUuid)&&brokerDate(v.statistics_date)&&text(v.source_contract_version)&&clock(v.first_seen_at)&&clock(v.ingested_at)&&nullable(v.source_published_at,clock)&&jsonRecord(v.metadata);}
export function isBrokerMetricResult(v:unknown):v is BrokerMetricResult {
  if(!brokerObject(v)||!BROKER_ALGORITHMS.includes(v.metric as BrokerAlgorithm)||!STATUSES.has(String(v.status))||!exactJson(v.value)||!nullable(v.formula,text)||!jsonRecord(v.parameters)||!nullable(v.unit,text)||!strings(v.reasons)||!Array.isArray(v.expected_dates)||!v.expected_dates.every(brokerDate)||!Array.isArray(v.missing_dates)||!v.missing_dates.every(brokerDate)||!Array.isArray(v.input_refs)||!v.input_refs.every(inputReference)||!jsonRecord(v.details)||!Array.isArray(v.selected_branch_ids)||!v.selected_branch_ids.every(brokerUuid)||typeof v.provisional!=="boolean"||typeof v.point_in_time_supported!=="boolean"||(v.algorithm_version!=="1.0.0"&&v.algorithm_version!==null)||v.engine_version!=="broker-flow-engine-1.0.0"||!hash(v.reproducibility_key))return false;
  const r=v.request;
  if(!brokerObject(r)||![r.instrument_id,r.market_id].every(brokerUuid)||r.metric!==v.metric||!brokerDate(r.start)||!brokerDate(r.end)||r.start>r.end||!clock(r.query_as_of)||!jsonRecord(r.parameters)||!["observed_revision_v1","published_and_observed_revision_v1"].includes(String(r.availability_policy))||!text(r.timezone_name)||r.engine_version!==v.engine_version||!sameJson(r.parameters,v.parameters))return false;
  if(v.status!=="complete"&&v.value!==null)return false;
  if(v.status==="complete"&&["shares","units","percent","fraction","source_currency"].includes(String(v.unit))&&!Array.isArray(v.value)&&!brokerObject(v.value)&&!brokerDecimal(v.value))return false;
  if(v.status==="complete"&&v.metric==="broker.top_branches"){
    const rows=v.value;
    if(!Array.isArray(rows)||!rows.every(rankRow)||new Set(rows.map(row=>row.branch_id)).size!==rows.length||new Set(rows.map(row=>row.rank)).size!==rows.length||rows.some((row,index)=>index>0&&row.rank<=rows[index-1].rank))return false;
  }
  if(v.status==="complete"&&v.metric==="proxy.broker_top_n_net_imbalance"){
    if([v.details.selected_buy_quantity,v.details.selected_sell_quantity].some(value=>value!==undefined&&(!brokerInteger(value)||value.startsWith("-")))||v.details.selection_basis!==undefined&&v.details.selection_basis!=="union_of_top_net_buyers_and_sellers")return false;
  }
  if(v.status==="complete"&&(String(v.metric).startsWith("broker.active_")||String(v.metric).startsWith("broker.net_"))){
    const signed=String(v.metric).endsWith("count_difference");
    if(!Array.isArray(v.value)||!v.value.every(row=>brokerObject(row)&&brokerDate(row.trade_date)&&typeof row.value==="number"&&Number.isSafeInteger(row.value)&&(signed||row.value>=0)))return false;
  }
  return v.input_refs.every(ref=>ref.instrument_id===r.instrument_id&&ref.market_id===r.market_id);
}
export function isBrokerMetricsResponse(v:unknown):v is BrokerMetricsResponse{return envelope(v)&&brokerObject(v)&&isBrokerMetricResult(v.result)&&v.result.request.instrument_id===v.instrument.id&&v.result.request.market_id===v.instrument.market.id&&v.result.input_refs.every(ref=>ref.metadata.quantity_unit===undefined||ref.metadata.quantity_unit===v.instrument.quantity_unit)&&Array.isArray(v.sources)&&v.sources.every(source);}
const counts=(v:unknown):v is Record<string,number>=>brokerObject(v)&&Object.values(v).every(count);
function sameJson(left:BrokerJson,right:BrokerJson):boolean {if(left===right)return true;if(Array.isArray(left)&&Array.isArray(right))return left.length===right.length&&left.every((item,index)=>sameJson(item,right[index]));return brokerObject(left)&&brokerObject(right)&&Object.keys(left).length===Object.keys(right).length&&Object.entries(left).every(([key,item])=>Object.hasOwn(right,key)&&sameJson(item as BrokerJson,right[key] as BrokerJson));}
function run(v:unknown):v is BrokerRun{return brokerObject(v)&&brokerUuid(v.id)&&text(v.status)&&clock(v.started_at)&&nullable(v.finished_at,clock)&&counts(v.counts)&&nullable(v.safe_failure_code,text);}
function sourceStatus(v:unknown):v is BrokerSourceStatus {return brokerObject(v)&&source(v.source)&&[v.canonical_count,v.revision_count,v.raw_count].every(count)&&counts(v.audit_counts)&&coverage(v.coverage)&&nullable(v.latest_canonical_date,brokerDate)&&[v.latest_source_published_at,v.last_seen_at].every(x=>nullable(x,clock))&&nullable(v.latest_run,run)&&brokerObject(v.capabilities)&&v.capabilities.automated_ingestion_enabled===false&&v.capabilities.public_display_verified===false&&v.capabilities.derived_display_verified===false&&v.capabilities.license_status==="blocked";}
export function isBrokerStatusResponse(v:unknown):v is BrokerStatusResponse{return brokerObject(v)&&v.product==="broker_flow_daily"&&v.frequency==="1d"&&Array.isArray(v.items)&&v.items.every(sourceStatus)&&count(v.count)&&v.count===v.items.length&&clock(v.read_at)&&usage(v.usage)&&typeof v.truncated==="boolean";}
function rankRow(v:unknown):v is BrokerRankRow{return brokerObject(v)&&brokerUuid(v.branch_id)&&[v.buy_quantity,v.sell_quantity].every(x=>brokerInteger(x)&&!x.startsWith("-"))&&brokerInteger(v.net_quantity)&&count(v.rank)&&v.rank>0&&Array.isArray(v.identity_ids)&&v.identity_ids.every(brokerUuid)&&[v.buy_amount,v.sell_amount].every(x=>x===undefined||x===null||brokerDecimal(x));}

/** Select only backend-complete ranks. No aggregation, subtraction or re-ranking. */
export function brokerRankRows(result:BrokerMetricResult):BrokerRankRow[]{return result.status==="complete"&&result.metric==="broker.top_branches"&&Array.isArray(result.value)&&result.value.every(rankRow)?result.value:[];}

/** Transform backend daily output for pixels; missing/blocked values stay absent. */
export function brokerDailyPoints(result:BrokerMetricResult):ChipDatum[]{
  if(result.status!=="complete")return [];
  const rows=result.metric==="proxy.broker_daily_top_n_imbalance_sum"?result.details.daily:result.value;
  if(!Array.isArray(rows))return [];
  const points:ChipDatum[]=[];
  for(const row of rows){if(!brokerObject(row)||!brokerDate(row.trade_date))return [];
    const value=result.metric==="proxy.broker_daily_top_n_imbalance_sum"?row.proxy_net:row.value;
    if(value!==null&&!(brokerDecimal(value)||typeof value==="number"&&Number.isSafeInteger(value)&&String(result.unit).includes("branch")))return [];
    points.push({date:row.trade_date,value:value===null?null:String(value),status:result.status});}
  if(new Set(points.map(p=>p.date)).size!==points.length)return [];
  return points.sort((a,b)=>a.date.localeCompare(b.date));
}

/** Dated labels originate only from server input references, never name guesses. */
export function brokerInputIdentities(result:BrokerMetricResult):BrokerIdentity[]{
  const values:BrokerIdentity[]=[];
  for(const ref of result.input_refs){const rows=ref.metadata.identities;if(!Array.isArray(rows))continue;
    for(const row of rows){if(!brokerObject(row))continue;const candidate={...row,identity_id:row.identity_id??row.id};if(identity(candidate)&&!values.some(x=>x.identity_id===candidate.identity_id))values.push(candidate);}}
  return values;
}
