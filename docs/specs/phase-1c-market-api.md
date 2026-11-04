# Phase 1C — Stock Market Query API

## 範圍與架構

新增唯讀 REST API、Application Service、Domain read projections／Port、PostgreSQL query adapter、Pydantic 回應模型與 OpenAPI contract。**未修改既有核心 Domain／ORM Model，沒有新增 Migration**。沒有前端、行情抓取、排程或技術指標。

- `api/routes.py`：同步 FastAPI routes、輸入驗證與安全錯誤轉換。
- `api/schemas.py`：所有 public response／error schemas；Decimal 使用 JSON 字串。
- `api/dependencies.py`：組裝本模組 query adapter；不跨模組存取私有 infrastructure。
- `application/query_service.py`：搜尋、範圍限制、聚合與 freshness/status use cases，僅依賴 Domain Port。
- `application/aggregation.py`：純函式的市場本地交易日聚合。
- `domain/queries.py`：唯讀 DTO、Repository Protocol、查詢錯誤與上限。
- `infrastructure/query_repository.py`：只查 canonical／market／source／calendar／run metadata，不讀 raw body、不 join revision 放大行情筆數。

`main.py` 是 composition root；lifespan 建立 lazy engine，未連線也能啟動 liveness。查詢在 thread pool 執行同步 DB I/O，每個 request 使用 `REPEATABLE READ` + `READ ONLY` transaction，statement timeout 10秒、lock timeout 3秒。多次搜尋 count/page 或資料狀態 SQL 使用同一 snapshot。Query adapter 不 commit、不 autoflush，所有條件 parameterized；沒有 provider HTTP 呼叫。

## API

Prefix：`/api/v1`。無驗證／授權機制的變更，沿用本機 Foundation 部署範圍。

| Endpoint | 行為 |
| --- | --- |
| GET /instruments | q 搜尋代碼或名稱；symbol、name 分別搜尋；market 篩選；分頁 |
| GET /instruments/{id} | UUID 證券詳細資料與市場 metadata |
| GET /stocks/{id}/bars | 1d／1w／1M、from/to、OHLCV、來源、覆蓋程度與 freshness |
| GET /stocks/{id}/latest | 最新**已保存**日行情，不是即時報價 |
| GET /data/status | 最新保存日期、各市場資料範圍與各來源同步狀態 |

`id` 一律是 instrument UUID，不能只用 symbol 識別跨市場證券。`/instruments` 可回傳既有的其他 instrument type；`/stocks/...` 限 STOCK，ETF 等證券在此回傳404。

### 搜尋與分頁

- `q`：代碼或名稱的 case-insensitive literal contains；最多100字。
- `symbol`：literal contains，最多32字；`name` 最多200字。
- 所有不同 filter 用 AND 組合；單獨 q 內部是 symbol OR name。
- `%`、`_`、反斜線是字元而非使用者可注入的 LIKE pattern；不支援 SQL 或 regex 搜尋語法。
- market 忽略輸入大小寫並正規化成大寫 code，最多32字；不在資料庫的合法 code 回傳200與空頁。
- 空白搜尋／不合法 code 回傳422。page=1..1000，page_size=1..100，預設1與50。
- 穩定排序：market.code、instrument.symbol、instrument.id。超過最後一頁回200空items；回應含 total、page、page_size、total_pages。

### 日行情查詢限制

- timeframe 僅接受 **1d、1w、1M**，大小寫有別，1m不合法。
- from／to 是 inclusive 的市場本地日期。to 預設市場現在日期，from 預設 to 往前365天，共最多366個日曆日。
- 單次 inclusive range 最多 **3660 日曆日**；輸入日行情防禦上限4000筆。
- limit=1..1000，預設1000，是聚合後回應 bar 上限；超過時回422，**不截斷、也不回傳半份歷史**。
- 支援純未來日期的空查詢，但不補造 future bar。無法表示完整週界的日期（如9999-12-31／1w）回422。
- 只聚合 from/to 之內的實際 daily bars；週／月範圍邊界外的行情不偷偷併入。日曆查詢會涵蓋完整週／月，以辨別是否截掉開市 session。
- 已存在股票沒有行情時，bars 回200與空陣列；latest 回200與 `bar=null`、freshness=no_data。不存在的股票回404。

## 聚合規則

以 `DailyBar.trade_date` 分組，不以 ingested_at 的 UTC 日期代替交易日。價格一律是 Phase 1A 保存的**未還原權息**價格。

| timeframe | period_start / period_end |
| --- | --- |
| 1d | 原交易日 |
| 1w | 該本地日期所在週的星期一至星期日；跨年度仍按同一 Monday date 分組 |
| 1M | 該本地日期所在月份第一日至最後一日，包含閏年 |

對每個非空 group，先按 trade_date 排序：first open、max high、min low、last close、sum volume_shares。turnover／trade_count 只在**全部**日資料都有值時加總；任何NULL使該合計為NULL，不能當0。全部已知為0時合計仍是0。

Decimal 加總採 local precision=64，不受呼叫端 Decimal context 影響；金融數值不經 float。volume_shares／trade_count 使用 Python整數。API 的 Decimal 價格／金額序列化為 JSON 字串，count／股數為 JSON integer；消費端需保留整數精度，不可自行以浮點處理超出其安全範圍的整數。

每個 bar 回傳：

- trade_date＝最後一個**實際觀察交易日**，不是假造的週日／月底行情。
- period_start/end、first_trade_date/last_trade_date。
- observed_session_count、expected_session_count、missing_trade_dates、calendar_missing_dates。
- sources：所有實際 constituent source 的 id/code/name/source_type，去重並穩定排序。
- last_ingested_at：各 constituent 的最大 ingested_at。

沒有資料的週／月不補零、不補前收價。Adapter 僅查 canonical PK；聚合再拒絕重複 date、混合 instrument 或錯誤 source metadata，防止 revision/audit join 重複計量。

## 期間結束與資料完整度是不同事實

`period_closed = period_end < market_local_today`，是保守的日曆界線，不猜各交易所收盤時間。因此當週即使星期五已收盤，也不會在週日結束前自動宣稱週期已結束。

`completeness`：

- **complete**：期間已結束，完整期間的每一日都有明確 Calendar record，全部 OPEN/HALF_DAY 都有 bar，且沒有 CLOSED 日 bar。
- **incomplete**：期間尚未結束，或已知開市 session 缺資料，或出現 CLOSED 日 bar。
- **unknown**：期間已結束，沒有上述已知缺口，但 Calendar coverage 不足。

只有 complete 才 `is_complete=true`；其他皆false並附原因。理由可含 period_in_progress、missing_sessions、query_clipped_sessions、unexpected_closed_session、calendar_unknown。

查詢只切掉已明確 CLOSED 的週末不造成不完整；切掉 OPEN session 才列為已知 missing。未知日期不自動視為週末或假日。`expected_session_count=null` 表示 Calendar 不完整，不能填入「通常一週五天」。

這是對**市場 Calendar coverage**的保守核對，不保證每個缺失日真的遺漏了該股票交易。IPO、停牌或無成交可能合法沒有 bar；現有核心模型沒有完整歷史個股交易狀態，因此不會藉此宣稱已涵蓋完整生命週期。Calendar record 是系統明確保存的 session 狀態；本 Phase 不建立或假裝已校驗官方交易日曆。

`history.available_from/to` 只描述目前保存的首末日期；`history.completeness` 永遠是 `not_verified`。局部期間完整不等於全部歷史完整。

## 參考價、前交易日與漲跌幅

`previous_observed` 以 `trade_date < first_observed_date ORDER BY trade_date DESC LIMIT 1` 取得，可能是星期五而不是星期日，也可能與本期有資料缺口；名稱明確表示**最近保存的觀察**。

`previous_session_verified` 核對的是相鄰**市場開市 session**：前筆與本筆日期明確 OPEN/HALF_DAY，且中間日期全部 CLOSED，才為true；已知中間仍有OPEN則false；日曆不明或間隔超過3660天則null。這不證明停牌股票的實際前一成交日，也不能代替除權息參考價。

現有核心資料只存未還原 OHLCV，未保存可核實的當日官方比較基準與完整公司行動。因此本 Phase 的 `reference_price`、`change`、`change_percent` **一律null**，原因 `official_reference_not_available`。不拿前一日曆日、任意舊 close、close−Change 或 NextReferencePrice 假算正式漲跌幅。

[TWSE 每日收盤行情說明](https://www.twse.com.tw/zh/trading/historical/mi-index.html) 對漲跌比較及無比價情況另有規定；[TPEx 官方 FAQ](https://www.tpex.org.tw/zh-tw/about/company/faq.html) 說明參考價可能不等於前收，除權息也有專門規則。Phase 1B Swagger 欄名不足以驗證上述語意，故未擅改 core model 或導入未查證參考價格。

## Freshness 與同步狀態

- freshness.as_of：API 評估時間；market_local_date 依 Market.timezone 計算。
- latest_trade_date／last_ingested_at：該股票最新保存交易日及**該日 bar**的匯入時間，獨立於目前查詢範圍。
- age_calendar_days 是日曆日差，並非缺少交易日數；不能因週末經過幾天就斷言 stale。
- is_realtime=false、data_kind=daily_snapshot；assessment 是 no_data、future_dated 或 calendar_not_verified。後者表示尚未驗證「最新應發布的完整 session」，即使某一過去期間的 Calendar 完整也不等於來源現在已更新。

`/data/status` 回傳全庫 latest_trade_date，但同時逐市場列出日期，不能把 TPEX 的較新日期套用到 TWSE。Sources 包含：

- latest_run：最後任何作業，包含 replay。
- latest_sync_run：最後一次 mode=sync 的外部同步，replay 不掩蓋同步失敗。
- latest_successful_sync_at：確實 succeeded 的 sync 才有值，不把 partial failed 冒充成功。
- has_partial_data、failure_code＝partial_import／sync_failed；原始自由文字 error_summary 不對外回傳。
- source.latest_trade_date 是目前 canonical 的資料日；last_ingested_at 是該來源所有 canonical 的最後匯入時間，不是即時行情時間。

狀態預設最多100個市場與100個來源，limit=1..100可縮小；提供 total_markets、total_sources、truncated，絕不靜默宣稱列表完整。SQL使用固定最多9個集合查詢，不隨 bar 數量執行逐筆 I/O。

## HTTP 錯誤

| HTTP | 原因 |
| --- | --- |
| 200 | 成功；搜尋空頁、無行情的既有股票亦是合法空結果 |
| 404 | instrument/stock 不存在；非STOCK不能作為stock路徑使用 |
| 422 | UUID／日期格式／timeframe／分頁錯誤、from>to、超過範圍或筆數上限 |
| 503 | DB不可用／timeout、儲存資料不一致等，回安全錯誤碼而非SQL、DSN或原始例外 |

所有上述錯誤均有 Pydantic ErrorResponse。validation errors 只包含loc、message、type，不回顯input。

## OpenAPI contract

- Runtime：<http://localhost:8000/openapi.json>；互動文件：<http://localhost:8000/docs>。
- 可審查 snapshot：`apps/api/openapi.json`，有測試比對與 runtime 相等。

```bash
# repository root，使用已安裝API套件的Python
.venv/bin/python apps/api/scripts/export_openapi.py
.venv/bin/python apps/api/scripts/export_openapi.py --check
# Docker內只檢查，不需要資料庫連線
docker compose run --rm backend python scripts/export_openapi.py --check
```

只修改routes/schemas後重新產生，不手工編輯JSON；export不啟動lifespan、不讀行情。

## 操作範例

先啟動既有 Compose 與資料庫 Migration（本 Phase 沒有新 revision）：

```bash
docker compose up --build --wait --wait-timeout 300
docker compose run --rm backend alembic check
curl 'http://localhost:8000/api/v1/instruments?q=2330&market=TWSE&page=1&page_size=20'
curl -G 'http://localhost:8000/api/v1/instruments' --data-urlencode 'name=台積電'
```

從 `items[].id` 取得UUID，以下 `<UUID>` 替換為實際值：

```bash
curl 'http://localhost:8000/api/v1/instruments/<UUID>'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1d&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1w&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1M&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/latest'
curl 'http://localhost:8000/api/v1/data/status'
```

行情查詢只讀目前 PostgreSQL。要更新資料仍須獨立執行 Phase 1B CLI，不會由上述請求觸發。

## 測試

```bash
docker compose run --rm backend python -m pytest -m 'not integration and not network' -p no:cacheprovider
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m 'not network' -p no:cacheprovider
```

涵蓋未知股票、空資料、日期／筆數／分頁上限、跨月份／年度／ISO週、閏年、無交易日、完整／不完整／未知calendar、查詢截斷、重複資料防護、Decimal精度、前筆session、HTTP errors、OpenAPI及真實PostgreSQL唯讀交易。DB測試使用獨立隨機schema；不把fixture寫入正式資料。

## 實際驗收（2026-11-04，Asia/Taipei）

| 驗證 | 結果 |
| --- | --- |
| 本機完整後端測試，RUN_INTEGRATION=1、排除network | **504 passed，2 deselected** |
| 最終 Docker image 內相同測試 | **504 passed，2 deselected** |
| 前端回歸 | Vitest **7 passed**、TypeScript／production build 通過，未修改前端 |
| OpenAPI | export --check 通過；Pytest比對snapshot與runtime一致 |
| Schema | alembic check 無新差異；core ORM／Domain Model及既有Migration未修改 |
| Compose | 四服務 healthy；health／ready及既有首頁正常 |
| 真實HTTP查詢 | 五組API實測通過，錯誤範圍422、未知股票404 |

使用目前本機已保存的真實行情驗收，沒有重新向TWSE／TPEx抓取資料：

- TWSE 2330：最新保存交易日2026-11-03，close回傳JSON字串 `2295.00000000`。
- TPEX 6488：最新保存交易日2026-11-04，close回傳JSON字串 `619.00000000`。
- 兩檔的日／週／月查詢都能回應；未有完整Calendar及歷史時，is_complete=false，history.completeness=not_verified。
- 全庫 canonical 仍為 **1973** 筆，查詢沒有新增行情或匯入作業。
- data/status正確保留各市場不同日期；可同時呈現已存在的有效行情與最新同步失敗。當下TPEX顯示partial_import，TWSE最新sync為sync_failed，未被較早有效資料或replay假裝成成功。

外部網路測試兩項刻意由 `-m 'not network'` 排除，本Phase不新增供應商請求。既有Starlette/httpx TestClient仍有1項deprecation warning；遠端GitHub Actions未執行。

## 限制與後續

- 目前未有完整交易日曆、個股歷史上市／停牌狀態及可核實官方參考價；因此不宣稱完整歷史，也不計算缺乏依據的漲跌幅。
- 沒有新增資料抓取、UI、排程或技術指標。
- 本次未commit、push，完成後停止。
