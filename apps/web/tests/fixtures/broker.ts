/** SYNTHETIC H5 responses. Never a production seed or real coverage claim. */
import type { BrokerHistoryResponse, BrokerLatestResponse, BrokerMetricsResponse, BrokerMetricResult, BrokerReport, BrokerStatusResponse } from "../../src/lib/broker-types";
import { instrumentFixture } from "./market-api";

export const brokerRangeFixture={from:"2026-01-02",to:"2026-01-02"};
export const brokerIds={source:"00000000-0000-0000-0000-000000000102",scope:"00000000-0000-0000-0000-000000000106",branch:"00000000-0000-0000-0000-000000000001",broker:"00000000-0000-0000-0000-000000000107",identity:"00000000-0000-0000-0000-000000010001",revision:"00000000-0000-0000-0000-000000000201",observation:"00000000-0000-0000-0000-000000000202",run:"00000000-0000-0000-0000-000000000103",raw:"00000000-0000-0000-0000-000000000104"};
const at="2026-01-10T10:00:00Z";
export const brokerUsageFixture={automated_download_enabled:false,public_display_verified:false,derived_display_verified:false,assessment:"not_verified"} as const;
export function brokerReportFixture():BrokerReport{return {
  instrument_id:instrumentFixture.id,market_id:instrumentFixture.market.id,trade_date:brokerRangeFixture.to,
  scope:{id:brokerIds.scope,source_id:brokerIds.source,market_id:instrumentFixture.market.id,code:"SYNTHETIC-regular",version:"SYNTHETIC-scope-v1",session:"SYNTHETIC-session",participant_universe:"SYNTHETIC-complete",correction_basis:"SYNTHETIC-reported",description:"SYNTHETIC only",odd_lot_included:false,block_trade_included:false,verified:true},
  quantity_unit:"shares",report_kind:"branch_daily_totals",price_basis:"raw",price_quantity_basis:"per_canonical_unit",currency:"TWD",completeness:"complete",branch_universe_complete:true,total_branch_count:1,filtered:false,
  trades:[{branch_id:brokerIds.branch,identity_id:brokerIds.identity,identity:{identity_id:brokerIds.identity,broker_id:brokerIds.broker,branch_id:brokerIds.branch,broker_code:"000A",branch_code:"000AB",branch_name:"SYNTHETIC 測試分點",identity_version:"SYNTHETIC-identity-v1",valid_from:"2020-01-01",valid_to:null,verified:true},quantity_unit:"shares",buy_quantity:"1000",sell_quantity:"100",reported_net_quantity:"900",net_quantity:"900",buy_amount:"20000",sell_amount:"2000",net_amount:"18000"}],
  observation_id:brokerIds.observation,promotion_policy_version:"SYNTHETIC-promotion-v1",date_evidence_reference:"SYNTHETIC-official-date-proof",
  timing:{trade_date:brokerRangeFixture.to,source_published_at:null,source_generated_at:null,first_seen_at:at,ingested_at:at},
  lineage:{source:{id:brokerIds.source,code:"SYNTHETIC_SOURCE",name:"SYNTHETIC 分點來源"},revision_id:brokerIds.revision,source_id:brokerIds.source,run_id:brokerIds.run,raw_payload_id:brokerIds.raw,payload_sha256:"a".repeat(64),source_namespace:"SYNTHETIC",source_contract_version:"SYNTHETIC-contract-v1",schema_version:"SYNTHETIC-schema-v1",parser_version:"SYNTHETIC-parser-v1",identity_policy_version:"as_reported_branch_v1"},
  quality:{status:"complete",reasons:[],provisional:true,point_in_time_supported:false},calculations:[{algorithm_id:"broker.branch_net_quantity",algorithm_version:"1.0.0",formula:"SYNTHETIC buy_quantity - sell_quantity"}],
};}
const coverage=()=>({stored_from:brokerRangeFixture.from,stored_to:brokerRangeFixture.to,stored_report_count:1,completeness:"not_verified" as const,expected_dates_known:false as const,known_missing_dates:[],truncated:false});
const freshness=()=>({latest_trade_date:brokerRangeFixture.to,latest_source_published_at:null,first_seen_at:at,ingested_at:at,age_calendar_days:8,assessment:"not_verified" as const,is_realtime:false as const});
export function brokerHistoryFixture():BrokerHistoryResponse{return {instrument:structuredClone(instrumentFixture),product:"broker_flow_daily",frequency:"1d",read_at:at,usage:{...brokerUsageFixture},...brokerRangeFixture,items:[brokerReportFixture()],count:1,limit:50,next_cursor:null,freshness:freshness(),coverage:coverage()};}
export function brokerLatestFixture():BrokerLatestResponse{const {from:_from,to:_to,items,count:_count,limit:_limit,next_cursor:_cursor,...shared}=brokerHistoryFixture();return {...shared,point:items[0]};}
export function brokerMetricFixture(overrides:Partial<BrokerMetricResult>={}):BrokerMetricsResponse{
  const report=brokerReportFixture();
  const result:BrokerMetricResult={
    request:{instrument_id:instrumentFixture.id,market_id:instrumentFixture.market.id,metric:"broker.top_branches",start:brokerRangeFixture.from,end:brokerRangeFixture.to,query_as_of:at,parameters:{top_n:15,side:"net_buyers"},availability_policy:"observed_revision_v1",timezone_name:"Asia/Taipei",engine_version:"broker-flow-engine-1.0.0"},
    metric:"broker.top_branches",value:[{branch_id:brokerIds.branch,buy_quantity:"1000",sell_quantity:"100",net_quantity:"900",buy_amount:"20000",sell_amount:"2000",rank:1,identity_ids:[brokerIds.identity]}],status:"complete",formula:"SYNTHETIC aggregate then rank",parameters:{top_n:15,side:"net_buyers"},unit:"shares",reasons:[],expected_dates:[brokerRangeFixture.to],missing_dates:[],
    input_refs:[{product:"broker_flow",instrument_id:instrumentFixture.id,market_id:instrumentFixture.market.id,statistics_date:report.trade_date,revision_id:brokerIds.revision,source_id:brokerIds.source,source_contract_version:"SYNTHETIC-contract-v1",first_seen_at:at,ingested_at:at,source_published_at:null,metadata:{scope:report.scope,identities:[report.trades[0].identity]}}],
    details:{},selected_branch_ids:[brokerIds.branch],provisional:true,point_in_time_supported:false,algorithm_version:"1.0.0",engine_version:"broker-flow-engine-1.0.0",reproducibility_key:"b".repeat(64),...overrides,
  };
  if(overrides.metric)result.request.metric=overrides.metric;
  if(overrides.parameters)result.request.parameters=overrides.parameters;
  return {instrument:structuredClone(instrumentFixture),product:"broker_flow_daily",frequency:"1d",read_at:at,usage:{...brokerUsageFixture},sources:[report.lineage.source],result};
}
export function brokerStatusFixture():BrokerStatusResponse{return {product:"broker_flow_daily",frequency:"1d",items:[],count:0,read_at:at,usage:{...brokerUsageFixture},truncated:false};}
