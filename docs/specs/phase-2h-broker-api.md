# Phase 2H-5 — Broker Flow Query API

實作／驗證日期：2026-11-18，Asia/Taipei。依據 [H1 指標定義](phase-2h-metric-definitions.md)、[H2 Persistence](phase-2h-broker-persistence.md)、[H3 Ingestion](phase-2h-broker-ingestion.md)及 [H4 Engine](phase-2h-broker-engine.md)。本次建立 Canonical 查詢、Application Services、公開 Reader、HTTP Schemas／Routes、OpenAPI、離線及隔離 PostgreSQL HTTP 測試；沒有開發前端或啟用下載。

## 1. 契約決策

原 [Phase 2 API Contract](phase-2-api-contract.md)沒有 Broker Flow 的正式路由；本階段新增下述五條 `/api/v1` 路由，不更換既有產品契約。Instrument 沿用 UUID，不以股票代碼當全域 ID。

H2 的 Scope 是帶來源、市場及版本的 UUID，因此 `trade_scope` 使用精確 Scope UUID，`source` 使用 DataSource UUID；不以顯示名稱、近似代碼或未核實的「一般／全部」字串互相替換。History 可以包含多個明確分開的報表；Latest／TOP／Metrics 若選到多個 Scope 則 422 `ambiguous_trade_scope`，不選其中最新者冒充同一序列。

使用者查詢的是合法保存的 Canonical 內部資料，不是授權發放介面。所有回應的 `usage` 保留 `automated_download_enabled=false`、`public_display_verified=false`、`derived_display_verified=false`、`assessment=not_verified`；Status 的對應能力仍為 Blocked。本次未取得新的來源權利、公開部署或啟用正式來源。

## 2. 路由與參數

| GET Route | 用途與參數 |
| --- | --- |
| `/api/v1/stocks/{id}/broker-flow` | `from/to`、`timeframe=1d`、`trade_scope/source`、`broker/branch` UUID、`limit/cursor/order`；完整日報分頁 |
| `/api/v1/stocks/{id}/broker-flow/latest` | `trade_scope/source`、`broker/branch`；最新已保存的單一 Scope 報表 |
| `/api/v1/stocks/{id}/broker-flow/top-branches` | `from/to`、`trade_scope/source`、`top_n=5/10/15/30`、`side=net_buyers/net_sellers`、可選 `broker/branch` 輸出篩選 |
| `/api/v1/stocks/{id}/broker-flow/metrics` | `algorithm`、`algorithm_parameters`、`from/to`、`timeframe=1d`、`trade_scope/source`、`branch`、可選 `broker`、`query_as_of`、`availability_policy` |
| `/api/v1/broker-flow/status` | 可選 `source` UUID、`limit=1..100`，預設 50 |

查詢上限與選擇規則：

- 市場限 TWSE／TPEX；支援 Canonical 所保存的 shares／units 證券，包含股票及其他證券，不將所有商品假設為一張 1,000 股。
- 日期為市場本地 `YYYY-MM-DD`，inclusive；省略 `to` 為當地今天，`from=to−365日`。最多 3,660 個日曆日，禁止反向、未來或縮寫／時間戳冒充日期。
- History 預設 `limit=50`，最大 250，計報表而非 Trade 列。同份報表不能切成兩頁。Cursor key 為 `(trade_date,scope_id)`，支援 asc／desc；保留同日不同 Scope，綁定日期、全部篩選、Limit 與 Order。
- Cursor 只保留查詢條件，不宣稱跨 HTTP 請求 Snapshot 不變；每頁都有 `read_at`。
- 單次載入最多 1,000 份計算報表、20,000 筆分點 Trade。先讀 Metadata 計算 Budget，超限 422，要求縮小範圍；不先載入全市場 JSON 再截斷。History 的額外一筆分頁 Lookahead 也計入 Budget。
- 累計／Streak 最多往回載入 366 個日曆日；有核實日曆時累計只讀所需 N 個 Session。讀到邊界不推定 Streak 結束，沿用 Domain 的 truncated／missing 狀態。
- `algorithm` 為 H4 登錄的 24 個 ID Enum，其中真實中實戶／大戶流量仍為 Unsupported。`algorithm_parameters` 是最多 4,096 字元的嚴格 JSON Object；重複 Key、NaN／Infinity、未知欄位、不適用參數、bool 假冒 N 都拒絕。Decimal JSON Number 使用 Decimal 解析，無 float 過程；金融門檻建議以字串提供。
- `branch` 對應 H4 的 `branch_id`，若 JSON 也指定則必須一致。Metrics 的 `broker` 必須搭配明確 Branch，且不能與已保存身分矛盾；不建立「券商群組」新公式。

### 2.1 篩選不改變排名母體

History 的 Broker／Branch 篩選透過 SQL EXISTS 找出符合的日報，再在輸出選取分點；`total_branch_count` 保留原報表筆數，`filtered=true` 清楚表示這是篩選視圖。

TOP 一律用完整相容母體先跨日期彙總，再排序；Broker／Branch 只在排序後篩選輸出，保留原 Rank。`result.details.output_filter` 明示 `ranking_universe_filtered=false`、`ranks_preserved=true`，不能將篩選後幾家分點改稱完整市場 TOP。

## 3. 回應

所有 Schema 使用 Pydantic `extra=forbid`；Source Quantity 為有 BIGINT 範圍驗證的字串，金額為有限、具有精度界線的 Decimal 字串。H4 的 heterogeneous Result 使用禁止 float 的遞迴 JSON Schema，保留數量字串及小型 Count／Rank 整數。沒有在 API 複製金融公式。

### 3.1 History／Latest

共同 Envelope 包含 `instrument`、`product=broker_flow_daily`、`frequency=1d`、`read_at`、`freshness`、`coverage`、`usage`。History 另有 `from/to/items/count/limit/next_cursor`；Latest 是 `point` 或 null。

每份 Report 包含：

- `instrument_id/market_id/trade_date`、完整 `scope`（Code／Version／Session／Participant Universe／Correction Basis／Odd Lot／Block Flags）。
- `quantity_unit`、`currency`、原始 `price_basis/price_quantity_basis`、`report_kind`。
- `completeness`、`branch_universe_complete`、`total_branch_count`、`filtered`。
- Trades：分點 UUID、Identity UUID、當日有效券商／分點代碼與名稱、版本與有效期間；買進／賣出、來源直接報告的 Net、Domain derived Net、精確 Side Amount／Net Amount。缺值保持 null，0 是真正來源零值。
- `timing`：真正交易日、`source_published_at` nullable、`source_generated_at` nullable、`first_seen_at`、`ingested_at`；Timestamp 為 aware UTC 輸出，日期不改成 UTC 日。
- `lineage`：Source ID／Code／Name、Revision、Run、Raw UUID、Payload SHA256、Namespace、Contract／Schema／Parser／Identity Policy。
- Observation UUID、Promotion Policy、日期證據及 Quality；`calculations` 直接引用 H4 登錄的 Net Quantity／Amount 公式與算法版本。

不公開 Raw Body、Import Config、原始 SQL、DSN、任意 Error Summary 或 Source Request Endpoint。History 的 `first_seen_at` 使用該 Revision 已保存的最早合法觀察，不因重播而變成今天。

### 3.2 TOP／Metrics

共同 Envelope 包含 `instrument/product/frequency/result/sources/read_at/usage`。Result 保留 H4 的完整已序列化 Request、Formula、Algorithm／Engine Version、Parameters、Value／Status／Reasons、Window、Expected／Missing Dates、Input References、Selected Branch IDs、Provisional／PIT 與 Reproducibility Key。

引用包含真正 Input Revision、所選 Observation、Run／Raw、來源版本及當日分點 Mapping。公式直接呼叫 `BrokerFlowEngine`，不在 Application 或前端重新實作排名、累計、家數、Proxy、集中度。

非 Complete 的正式金融 Value 為 null；可採用的來源觀察及診斷另留於 Details。不完整母體不能當完整 TOP15；中實戶真實流量沒有假算法版本。TOP 不足 N 時只回實際正／負分點，零 Net 不補榜。

### 3.3 Status

只列有 Broker Source Audit 的已保存來源，不虛構 DataSource UUID。無來源時 items 為空，頂層 Usage 仍顯示未核實。

每來源包括 Canonical／Revision／Raw 數量、Report Audit disposition counts、保存範圍、最新 Canonical 日期、已知發布／觀察時間、Latest Run。Run Statistics 重用 H3 `IMPORT_COUNTERS` 白名單，保留 accepted／partial／rejected／unresolved 的 Rows／Reports；不回傳未知鍵或負值。任意來源錯誤摘要替換為固定 `ingestion_failed`，避免洩漏內部訊息。

Raw Count 不等於成功報表數，Revision Count 不等於交易日覆蓋數；部分或被拒絕報表不會出現在 Canonical History。Status 沒有啟用任何下載功能。

## 4. Calendar、Freshness 與 As-of

正式 `trading_calendars` 尚無核實範圍、版本及來源證據。預設 Reader 不把行情日期、已載入分點日報或週一至五推算成交易日曆；多日完整排名、N-session、Streak 會保留 `not_verified`／null。

`BrokerCalendarReader.load(market_id,start,end,query_as_of)` 是伺服器端公開 Port，可由已核實 Evidence 實作注入；HTTP 不接受 `calendar_verified=true` 作授權或證明。隔離測試使用明確 SYNTHETIC Calendar 驗證數值，不當正式日曆。

Freshness 提供最新已保存日、資料年齡與時間，但無核實發布 SLA 時是 `not_verified`，不任意標為 Stale 或即時。Coverage 的兩端及已保存筆數不是中間完整性的證明，`expected_dates_known=false`。

Metrics 可指定 aware `query_as_of`，不得晚於伺服器本次時刻；預設範圍也以該時刻的市場本地日為準。重用 H4 `observed_revision_v1` 或 `published_and_observed_revision_v1`：未知發布時間不補值，嚴格 Policy 會阻擋；未觀察／未匯入的 Revision 不能提前使用。

Reader 只讀當前 Canonical，不提供歷史 Canonical 選擇時間線。若查過去 As-of，當前 Revision 當時尚不可用則回 not_yet_available，不回頭猜一版替代，也不宣稱 PIT Backtesting 保證。預設 `point_in_time_supported=false`，已知 Source Published Time 本身不足以改成 true。

`BrokerPriceReader` 可透過公開 Port 注入；正式組裝目前沒有 Market Data Price Reader，因此 `broker.flow_price_alignment` 會保留缺價／Unavailable 狀態。沒有跨模組私有 ORM、推算調整價、以收盤價估成交額或捏造比較曲線。

## 5. Persistence 與安全邊界

Reader 只在自己的模組查 Canonical 所指的 Observation／Revision，驗證其 typed keys、版本、單位、日期與有效 Identity；Market／Instrument／DataSource／Run 經 `market_data` 公開 Ports 讀取。不同來源同日分開，整份報表不拼接 Revisions。

每次 GET 由 Dependency 建立一個 Session：

```sql
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '3s';
```

同一 Session 讀取 Instrument、Canonical、Identity、Source 及 Coverage。另一個 Writer 在請求途中提交新 Revision，當次結果仍保留舊 Snapshot，下次請求才看見新版。每次 Request 都釋放 Session，不執行 Commit 寫入、Migration、外部 HTTP 或更新 Canonical。

既有 `(instrument_id,trade_date,scope_id)` 主鍵支援日期範圍；以 Scope／Source、Trade／Identity FK 與 EXISTS 保留身分隔離。Metadata 先驗 Budget 才讀 Report JSON；不讀 Raw Body、Group JSON 或 Config JSON。

HTTP 語意：

| HTTP | 情境 |
| --- | --- |
| 200 | 已知證券有資料或空資料；Latest 空為 point=null；不完整／不支援指標有狀態與 null |
| 404 | 證券不存在，或不支援市場／單位 |
| 422 | 無效日期／UUID／參數／Cursor／頻率／As-of、模糊 Scope、超出報表／分點 Budget |
| 503 | DB 不可用、缺必要 Schema、儲存內容不符合契約；錯誤訊息去敏 |

上游來源失敗而舊 Canonical 正常可讀時仍回 200，不會因 GET 發起重試下載。

## 6. OpenAPI 與測試

`apps/api/openapi.json` 由正式 Runtime 產生；新增五個 Paths 與 Broker 專用 Enum／Schemas。逐項比較前一版本，既有 Paths 與 Schemas 沒有語意變更。API Enum 值由 Domain Enum／Registry 產生，避免同名型別導致其他產品契約重新命名。

```bash
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
.tools/api-venv/bin/python -m pytest apps/api/tests/test_broker_flow_api.py -q
.tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration and not network' -q

# 獨立臨時資料庫；各測試再建私有 Schema，不操作正式行情／籌碼資料
cd apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/phase-2h5 -- \
  tests/test_broker_flow_query_postgres.py tests/test_broker_flow_postgres.py \
  tests/test_broker_flow_concurrency_postgres.py tests/test_broker_flow_ingestion_postgres.py \
  tests/test_market_data_postgres.py -q --tb=short
```

新增測試全部為明確 SYNTHETIC 資料，沒有作正式 Seed。驗證內容包括：

- 空資料、404、日期／Cursor／As-of 錯誤、參數白名單、HTTP 錯誤去敏。
- BIGINT 字串、Decimal 門檻、Null／Zero、當日有效名稱／代碼、發布時間未知。
- 不同來源／Scope 同日獨立、完整報表分頁、先彙總再排序、後置分點篩選不改 Rank。
- 真實 PostgreSQL Canonical→Repository→Service→HTTP；Revision／Duplicate／Stale Replay／Rollback。
- 注入核實 Synthetic Calendar 的 5-session 累計 500、Buy Streak 3；缺分點不補零，預設日曆不冒充核實。
- 真正另一個 Writer 提交期間的 RR Snapshot；Read-only、Metadata Budget、GET 不讀 Raw／不連 Provider。
- Status Statistics 白名單、公式／版本、OpenAPI 精確數值與 Limits、全部既有非整合回歸。

本次實測：

| 檢查 | 結果 |
| --- | --- |
| 新增 API Unit／Contract | 35 passed（包含於完整 Backend 回歸） |
| 新增 PostgreSQL HTTP Integration | 首輪 27 passed，27.87 秒；臨時 DB 已移除 |
| H5／H2／H3／Phase 1 PostgreSQL 回歸 | 189 passed，186.28 秒；臨時 DB 已移除 |
| Backend 非網路／非整合 | 3,294 passed；600 integration／network deselected |
| OpenAPI Export --check | 通過；既有 Paths／Schemas 未改變 |
| Vitest | 20 files、352 tests passed |
| Typecheck／Production Build | 通過 |
| 現有 Docker Health／後端 /health | 既有五個服務 healthy；/health 成功 |

本機 Log 位於忽略目錄 `.tools/validation/phase-2h5/`。既有 Starlette/httpx 棄用提示未當成失敗，也未作無關依賴升級。沒有 Compose／連線設定變更，沒有重建執行中服務或啟動新的長期 Worker。

修改檔案包括 `broker_flow/api/{routes,schemas,dependencies}.py`、`application/query_service.py`、`domain/query.py`、`infrastructure/query_repository.py`、`public.py`、模組說明、`stockapp/main.py`、`apps/api/openapi.json`、兩個新測試檔 `test_broker_flow_api.py`／`test_broker_flow_query_postgres.py`、README、本規格及共用 Phase 2 API Contract。沒有修改 H4 計算公式、Phase 1 Models、Migration、前端、Compose 或依賴版本。

## 7. 正式資料與啟用條件

本次正式 DB 唯讀複查：`0010_daily_sync`、`broker_flow_*` 表數 0，沒有套用正式 Upgrade 或匯入分點資料。沒有把 PostgreSQL 的 Synthetic 測試資料當真實覆蓋；正式分點資料驗收仍為 **Blocked／未執行**。既有行情、法人、集保、融資融券、借券資料沒有被本次寫入測試操作。

H5 不新增 DDL；實際啟用要讓 Backend 使用本階段程式，並先審查／安裝 H2／H3 的 `0011_broker_flow`、`0012_broker_ingestion`。操作指令見 [README](../../README.md)。Migration 只建立資料結構，不會取得資料權利或自動新增前端。

尚未取得正式來源及公開／衍生展示核可，沒有完整分點歷史或真實中實戶分類。正式交易日曆 Evidence 與公開 Price Reader 組裝仍是明確缺口。後續前端需沿用本 API 的狀態／空值／來源與 Proxy 名稱；本階段停止於 H5，不提前開發 H6、提交、Push 或公開部署。
