/** Same-origin canonical clients. No financial formulas or provider requests. */
import { ApiError, request } from "./market-api";
import { isInstitutionalHistoryResponse, isInstitutionalMetricsResponse, isOwnershipHistoryResponse, isOwnershipMetricsResponse, isMarginHistoryResponse, isLendingHistoryResponse, type ChipRange, type InstitutionalCategory, type LendingKind } from "./chip-types";

const invalid = () => new ApiError(502,"invalid_response","籌碼 API 回傳的資料與查詢條件不一致，無法安全顯示。");
function query(range: ChipRange, extra: Record<string,string> = {}, paged = true) {
  return new URLSearchParams({from:range.from,to:range.to,...(paged ? {limit:"1000",order:"asc"} : {}),...extra});
}
async function exact<T extends {instrument:{id:string};from:string;to:string}>(path:string,guard:(v:unknown)=>v is T,id:string,range:ChipRange,signal?:AbortSignal) {
  const data = await request(path,guard,signal);
  if(data.instrument.id!==id || data.from!==range.from || data.to!==range.to) throw invalid();
  return data;
}
export function getInstitutionalHistory(id:string,range:ChipRange,signal?:AbortSignal) {
  return exact(`stocks/${encodeURIComponent(id)}/institutional?${query(range)}`,isInstitutionalHistoryResponse,id,range,signal);
}
export async function getInstitutionalMetrics(id:string,category:InstitutionalCategory,range:ChipRange,signal?:AbortSignal) {
  const data=await exact(`stocks/${encodeURIComponent(id)}/institutional/metrics?${query(range,{category},false)}`,isInstitutionalMetricsResponse,id,range,signal);
  if(data.category!==category) throw invalid(); return data;
}
export function getOwnershipHistory(id:string,range:ChipRange,signal?:AbortSignal) {
  return exact(`stocks/${encodeURIComponent(id)}/ownership?${query(range)}`,isOwnershipHistoryResponse,id,range,signal);
}
export async function getOwnershipMetrics(id:string,threshold:number,range:ChipRange,signal?:AbortSignal) {
  if(!Number.isSafeInteger(threshold)||threshold<=0) throw new ApiError(422,"invalid_parameters","持股門檻必須是有效的基礎股數。");
  const data=await exact(`stocks/${encodeURIComponent(id)}/ownership/metrics?${query(range,{threshold:String(threshold),threshold_unit:"shares"})}`,isOwnershipMetricsResponse,id,range,signal);
  if(data.threshold!==String(threshold)||data.threshold_unit!=="shares") throw invalid(); return data;
}
export function getMarginHistory(id:string,range:ChipRange,signal?:AbortSignal) {
  return exact(`stocks/${encodeURIComponent(id)}/margin?${query(range)}`,isMarginHistoryResponse,id,range,signal);
}
export async function getLendingHistory(id:string,kind:LendingKind,range:ChipRange,signal?:AbortSignal) {
  const data=await exact(`stocks/${encodeURIComponent(id)}/lending?${query(range,{report_kind:kind})}`,isLendingHistoryResponse,id,range,signal);
  if(data.report_kind!==kind) throw invalid();return data;
}
