# Phase 2 — REST API Contract 草案

狀態更新：2026-11-10，Asia/Taipei。Phase 2B-5實作institutional、2C-2實作ownership、2D-2實作margin、2E-2實作Lending History／Latest／Status及shared chip-data/status的lending_daily分支（[借券實作規格](phase-2e-lending-api.md)）。Runtime OpenAPI／snapshot隨實作更新；泛用chip-metrics仍是未實作提案。

### Phase 2B-5 與早期草案的差異

- 新增 `/api/v1/stocks/{id}/institutional/metrics`，直接委派既有InstitutionalCalculator，period只允許5／10／20／60，與2F泛用metric的1–250提案分開。
- 不支援的證券／市場依本次任務回404，而非早期草案的422；目前法人產品只支援TWSE／TPEX的stock。
- `trade_scope` 可用code或包含五個欄位的嚴格JSON；同code不同flags／basis時須明確選完整scope，否則422。未知code視為無matching canonical的filter，回200空結果，不改接其他scope。
- 現有trading_calendars沒有已核實範圍、來源證據與版本，預設metrics calendar.verified=false、value=null／status=not_verified；不以行情列或週一至五猜日曆。verified calendar是server-side Port，不接受query自行宣告。
- TPEx只存在candidate時，canonical history／latest回空，source status揭露candidate及能力缺口。合法partial canonical的quality.status=partial；Domain not_verified保留獨立狀態與缺欄flags，不偽造complete。
- source quantity為嚴格BIGINT字串；derived value可超過BIGINT，使用50位整數Decimal字串。reported totals與derived_daily分開，補充分類不重複加總。
- 每次請求固定REPEATABLE READ READ ONLY snapshot；cursor只綁查詢條件，不宣稱跨請求revision snapshot。history單頁最多1000，metrics輸入最多1000份報表，日期區間最多3660日。

依據：[領域設計](phase-2-domain-model.md)、[官方來源能力](phase-2a-data-sources.md)、[實作順序](phase-2-roadmap.md)。下列 path 的 `{id}` 一律為既有 Instrument UUID，不是股票代碼；代碼搜尋沿用 `GET /api/v1/instruments`。

## 1. 共通規則

- 只讀 PostgreSQL 已保存的 canonical／指定 revision；不在 GET 內同步 TWSE、TPEx 或 TDCC。來源失敗但舊資料可用時回200及 freshness/quality，不把舊資料改成今天。
- 市場本地 ISO 日期 `YYYY-MM-DD`，inclusive `from/to`；每日與週報有各自的 `frequency` 和 `as_of_date`，禁止用同一個 timeframe 參數暗中重採樣。
- 價格、金額、比例用 Decimal 字串；新增 API 的 BIGINT 數量／人數也輸出十進位整數字串，避免 JavaScript 失真。page/count/limit 等小型 metadata 才用 JSON integer。此為新籌碼契約，**不改 Phase 1 的 JSON 整數欄位**。
- null 表示不可取得／不適用，搭配欄位或 point 的 `unavailable_reasons`；確定的 0 是 `"0"`，不以 null/空字串取代。數量必附 `quantity_unit=shares|units`，原始張數及 conversion lineage 可供稽核。
- 預設 `to` 為 instrument 市場本地今天，`from=to−365日`；範圍不代表資料已齊。to 超過當地今天、from>to、不合法日期或超上限回422。省略範圍不等於下載歷史。
- 預設 limit=250、最大1000；日／週報最多3660日曆日。ownership limit 計報表，不計級距列，不能將同份報表切成兩頁；每份上限64級距，超過視為儲存／契約異常，不能截斷。
- `order=asc|desc`，預設asc；cursor 是不透明、帶 query signature 的 keyset cursor，下一頁保留原篩選。跨頁 revision 更新可能改變結果，Phase 2 初版不宣稱跨請求 snapshot isolation；每頁提供 `read_at`。單次請求用同一個唯讀 DB snapshot。
- 不提供公開匯入／回放 POST；Phase 2B–2E 先用手動 CLI。未來需要管理 API 時另做身份驗證設計，不讓 GET 觸發副作用。

## 2. 路由與 Query Parameters

| 草案 GET route                             | query／用途                                                                                                      | 預定階段        |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- | --------------- |
| `/api/v1/stocks/{id}/institutional`        | from/to、`trade_scope`、limit/cursor/order；一個日期回完整 component 組，不以category篩選破壞對帳                | 2B              |
| `/api/v1/stocks/{id}/institutional/latest` | trade_scope；最新**已保存**一份，不宣稱最新官方／即時資料                                                        | 2B              |
| `/api/v1/stocks/{id}/institutional/metrics` | from/to、trade_scope、category、period=5/10/20/60或省略回四組；daily/cumulative/streak附Domain狀態與lineage | 2B-5 |
| `/api/v1/stocks/{id}/ownership`            | from/to（統計日期）、limit/cursor/order；完整週報級距                                                            | 2C              |
| `/api/v1/stocks/{id}/ownership/distribution` | as_of可選，指定統計日的完整分布；無資料不沿用其他日 | 2C-2 |
| `/api/v1/stocks/{id}/ownership/metrics` | from/to、threshold整數基礎量、threshold_unit=shares/units、limit/cursor/order；ratio／change_pp | 2C-2 |
| `/api/v1/stocks/{id}/ownership/latest`     | 最新已保存的 TDCC 統計週報                                                                                       | 2C              |
| `/api/v1/stocks/{id}/margin`               | from/to、`balance_scope`、limit/cursor/order；融資與融券兩個 leg                                                 | 2D              |
| `/api/v1/stocks/{id}/lending`              | from/to、`report_kind=borrowed_positions\|lending_short_sales`、trade_scope、lending_channel、limit/cursor/order | 2E              |
| `/api/v1/stocks/{id}/chip-metrics`         | from/to、metric、parameters（嚴格 JSON）、`evaluation_frequency=1d\|1w`；只接受 metric 支援的頻率                | 2F              |
| `/api/v1/chip-data/status`                 | product、market、source_code（可選）、limit≤100、cursor；每個 dataset 各自同步／授權／覆蓋狀態                   | 2B 起逐產品加入 |

scope 使用部署中 source contract 的明確 enum；初版只有一個已批准 scope 時可省略，由回應明示預設值。多 scope 存在且未指定時回422 `ambiguous_trade_scope`，不能選較新的一個當相同序列。無法核實的含鉅額／其他範圍為 unspecified；禁止拿來跨來源合計。

`chip-metrics.parameters` 白名單依 metric version：法人累計v1沿用正式InstitutionalCalculator，N僅5／10／20／60個確認交易session（原1–250的廣泛草案未實作）；ownership threshold 是級距可精確表達的整數股數；不能傳任意 SQL／公式。未知 key、bool冒充int、級距中間門檻、未支援的 frequency 在未來HTTP實作應回422。目前不提供跨商品綜合「主力分數」。

Phase2F-1已完成[純分析Engine與公開Ports](phase-2f-chip-engine.md)，含版本、50位Decimal／12位HALF_EVEN、Input Revision及發布／觀察cutoff。`/chip-metrics`仍為未實作的未來路由，這次不新增Pydantic／OpenAPI操作。單產品公式由其公開Calculator唯一維護，只有跨來源比例等新公式放chip_analysis；未核實的scope／Channel／分母相容性不產生比例。

## 3. Response Schemas 草案

Pydantic 使用 `extra="forbid"`，Decimal／BIGINT 的字串 serializer 必須有數值範圍驗證，不能只驗 regex 就接收任意長整數。Institutional對應模型已在2B-5建立（詳見 [實作契約](phase-2b-api.md)）；其餘產品型別仍是草案：

| Model                   | 必要欄位／語意                                                                                                                                                                                                   |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ChipSeriesResponse[T]` | instrument（沿用公開metadata）、product、frequency、from/to、items、count、next_cursor、read_at、freshness、coverage、source_contract_versions                                                                   |
| `FactTiming`            | as_of_date、source_published_on nullable、source_published_at nullable、first_seen_at、last_seen_at、ingested_at、timezone_name；UTC aware timestamps，as_of_date保留市場當地日期                                               |
| `FactProvenance`        | source{id,code,name}、revision_id、run_id、raw_id、source_row_locator、parser_version、source_contract_version、promotion_policy_version、normalized_hash；不公開 raw body 或含token的endpoint                   |
| `Quality`               | status=verified/partial/not_verified/quarantined、flags、unavailable_reasons；canonical 不回傳無效數值，quarantined 僅在status統計呈現                                                                           |
| `Freshness`             | latest_as_of_date、latest_source_published_at nullable、last_seen_at nullable、age_calendar_days nullable、assessment=no_data/not_verified/within_expected_window/delayed、expected_frequency、is_realtime=false |
| `Coverage`              | stored_from/stored_to nullable、completeness=not_verified/partial/verified_for_declared_scope、declared_scope、expected_dates_known、known_missing_dates、truncated；兩個端點不代表中間完整                      |

freshness 的 delayed／within_expected_window 只能在該來源已驗證發布日曆與發布 window 後使用；不能因週六看到週五統計就判 stale，也不能由「每週」推出固定星期五發布。無發布 SLA 或日曆時保守 not_verified，仍提供日期與 age。

`InstitutionalPoint`：trade_date、trade_scope（含原始成交／更帳basis）、timing、provenance、quantity_unit、components、quality。components 為 category + mapping_status + aggregation_role + buy/sell/net quantities；foreign_ex_dealers/investment_trust/dealer_proprietary/dealer_hedging是可相加leaf，foreign_dealers/foreign_inclusive為supplemental，dealer_total/three_major_total為subtotal。舊外資欄以foreign_unspecified／unverified表示；未知／未提供項為null及原因，不把補充外資自營商再次加入三大總計。source-total 與 derived-total 有明確欄位，公式版本可追溯。

`OwnershipPoint`：as_of_date、frequency=1w、timing、provenance、quantity_unit、bucket_scheme_version、denominator=depository_inventory、source_total_holders/quantity、buckets、quality。bucket 含 code/kind/label/lower_inclusive/upper_inclusive、holder_count、quantity、reported_percent。差異調整與合計仍可顯示，但不能當 regular holdings；保留來源原始調整值、blank或非0的adjustment holders，並回`adjustment_value_convention=shortfall_magnitude|signed_delta|unverified`，不能把CSV正差額直接當signed delta。週日曆標記不取代真正統計日。

`MarginPoint` 實作：trade_date、balance_scope、quantity_unit／source_quantity_unit、financing、short_margin、timing／provenance／quality。Financing 使用 previous_balance／purchase／sale／cash_repayment／balance；Short Margin 使用 previous_balance／short_sell／short_cover／stock_repayment／balance，避免通用 buy／sell 方向混淆。每欄 Quantity 有精確字串 value、unit、availability；附 signed_adjustment、公式政策／identity、daily_change、quota_limit 與 available_quota。限額 report_date／effective_on／effective_relation／basis 分開；未核實不以當日或 D+1 猜值。Provenance 保留 raw／run／revision、版本與 instrument-period ConversionEvidence。D+1 前日 assertion 不直接覆寫 D current。沒有用數量欄冒充金額。

Margin 路由為 `GET /api/v1/stocks/{id}/margin`、`GET /api/v1/stocks/{id}/margin/latest`、`GET /api/v1/margin/status` 及 `GET /api/v1/chip-data/status?product=margin_daily`。History 限制 3,660 日與 1,000 筆，支援日期 keyset cursor／order。Latest 無資料回 point=null。數值與 Decimal 為字串，GET 使用同一次 RR 唯讀快照且不連供應商。Daily Change 共用 Domain 公式 `balance − official previous_balance`，不是查前一曆日，亦非把餘額當 flow；coverage／freshness 在沒有 verified 日期覆蓋時維持 not_verified。完整實作契約以 [2D-2 規格](phase-2d-margin-api.md)及 OpenAPI 為準。

`LendingPoint` 實作：trade_date、report_kind、trade_scope、lending_channel（category/code/family/version/verified/coverage）、quantity_unit、typed ledger、quotas、timing／provenance／quality。Borrowing ledger使用previous_balance／borrowed_quantity／returned_quantity／signed_adjustment／balance；Lending Short使用previous_balance／short_sales／short_returns／signed_adjustment／balance。所有Quantity有value精確字串|null／unit／availability，Decimal亦為字串；不把不同kind的欄位補0，不提供未核實amount／currency。

Channel新增unspecified以忠實保存M6未確認的系統範圍，不能猜成combined。M4僅有SLBNLB候選契約且仍受權限／Schema gate阻擋，不能宣稱正式借券未還完成。Quota獨立保存purpose、report_date、effective_on／relation／basis，未知次一營業日不推算日期；不通用限制sold≤quota。quality區分identity complete與channel／scope not_verified，coverage／freshness保守not_verified。

路由為GET `/api/v1/stocks/{id}/lending`、`/lending/latest`、`/api/v1/lending/status`及shared status `product=lending_daily`。預設report_kind=lending_short_sales、trade_scope=unspecified；History用date/family/code複合cursor保留同日獨立Channel，最多3,660日／1,000筆；Latest選到多Channel回422 ambiguous_lending_channel。GET僅查Canonical、同一次RR唯讀Snapshot、不呼叫來源；aggregation=none，不加總每日balance作期間交易量。完整欄位與錯誤契約見[2E-2規格](phase-2e-lending-api.md)及OpenAPI。

`ChipMetricResponse`：instrument、metric、algorithm_version、parameters、evaluation_frequency、precision、points、coverage。每點含 value（Decimal string|null）、unit、status、reason、input_refs（原始報表日期／revision／可用時間）、availability_policy、is_provisional。ownership 變化單位 percentage_points；百分比變化是另一種公式，不能混稱。raw/adjusted price 若未使用則不假設存在；需要價格分母時明示 basis 及來源。

### 空資料的 JSON 形狀

下例為**契約形狀範例，無金融數字、無正式來源觀察**；實作時另由instrument與實際查詢填入 metadata：

```json
{
  "product": "ownership_weekly",
  "frequency": "1w",
  "items": [],
  "count": 0,
  "next_cursor": null,
  "freshness": {
    "latest_as_of_date": null,
    "latest_source_published_at": null,
    "last_seen_at": null,
    "age_calendar_days": null,
    "assessment": "no_data",
    "expected_frequency": "1w",
    "is_realtime": false
  },
  "coverage": {
    "stored_from": null,
    "stored_to": null,
    "completeness": "not_verified",
    "declared_scope": "verified_instrument_identity_only",
    "expected_dates_known": false,
    "known_missing_dates": [],
    "truncated": false
  }
}
```

這是省略 instrument/from/to/read_at/source_contract_versions 的 response **片段**，不是完整 fixture。未知 instrument 不回此空內容，應回404。

## 4. Status、錯誤與 lineage

`/chip-data/status` 依 product+source+market scope 回：

- source capabilities：latest/date_query/manual/archive、已查證起點與 retention、license_status=verified_open/contract_required/unresolved、automated_ingestion_enabled；文件起點、實測最早日與本平台已保存起點分開。
- latest reported_as_of_date／source_published_at／fetched_at；不能把 TDCC 日期、法人日期和行情日期合併成單一「最新籌碼日期」。
- latest_run{id,status,started_at,finished_at,has_partial_data,safe_failure_code,counts}；partial 沿用 failed + has_partial_data，不改現有 enum。
- last_successful_run、accepted/rejected/unresolved、known coverage gaps／未發布狀態；權限／授權未核可時顯示 disabled，不以全部0表示無交易。
- TDCC CSV 與 JSON 若日期不同，各自呈現來源日期；不將舊 JSON 貼成新一週，也不以兩者 union 補出假完整快照。

| HTTP | 情境／安全 code                                                                                                                                                                      |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 200  | 有資料或合法範圍無資料；latest 無資料則 point=null、freshness=no_data                                                                                                                |
| 404  | instrument_not_found；未實作的路由仍是目前一般404，不能在 Phase 2A 假裝已提供                                                                                                        |
| 422  | invalid_date_range、query_limit_exceeded、unsupported_instrument_product、unsupported_frequency、invalid_metric_parameters、ambiguous_trade_scope、historical_capability_unavailable |
| 503  | database_unavailable、invalid_stored_chip_data；不包含SQL、DSN、原始上游HTML或tokens                                                                                                 |

來源暫時故障只影響匯入狀態；已保存資料正常可讀時不是503。資料尚未匯入回200空序列；源頭根本不支援的管理端歷史任務應在發請求前拒絕，不能把最新資料改日期後寫入。

## 5. OpenAPI／測試規格

Phase 2B 起，每個新增 route 必須同步 runtime OpenAPI、`apps/api/openapi.json` 與 export `--check`；納入 tags、operationId、enum、nullable、精確數值字串格式、page限制及安全錯誤 model。使用現有 `scripts/export_openapi.py`，不手工改 runtime snapshot 來假裝有 route。

必要 contract tests：

1. UUID不存在／instrument無此產品、空序列、latest null、日期／cursor錯誤、超範圍、1000筆界線及ownership整份分頁。
2. 負net、BIGINT超JavaScript安全整數、Decimal比例精度、真0與null、不適用欄位；不出現float金融值。
3. Dealer hierarchy不重算兩次；法人16/19/24欄schema、多層重複欄頭、OpenAPI leading-space key與缺component不補0；週報不日化、跨年／非固定週五、資料發布日與統計日不同。
4. 原始日期／單位／source/revision/version完整；同日修訂、舊raw重播、CSV與JSON不同日期不混合。
5. 下游API失敗或空資料不觸發對外HTTP；使用DB repository與固定明示合成fixtures完成離線測試。
6. 真實PostgreSQL隔離schema測試 FK/CHECK/去重／transaction、read snapshot與Phase1無退化；network smoke另獨立且只於授權端點少量執行。

Institutional與Ownership可執行範例見README；ownership門檻使用基礎量，400000／1000000切穿級距回unsupported，400001／1000001可計算。正式週報schedule未核實時weekly change回not_verified／null。TDCC status是global dataset，market篩選回422；未知／不支援ownership市場回404。

```bash
curl 'http://localhost:8000/api/v1/stocks/<UUID>/ownership/latest'
curl 'http://localhost:8000/api/v1/chip-data/status?product=ownership_weekly'
```
