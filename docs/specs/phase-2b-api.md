# Phase 2B-5 — Institutional Trading REST API

狀態：2026-11-08，Asia/Taipei。只新增法人查詢 API、Application query service、read repository、公用 reference reader、契約及測試。沒有新表、Migration、外部下載、法人 UI 或 Phase 2B-6 功能。

依據：[Domain](phase-2b-institutional-domain.md)、[Persistence](phase-2b-persistence.md)、[Adapters](phase-2b-adapters.md)、[Ingestion](phase-2b-ingestion.md)、[共用 API 契約](phase-2-api-contract.md)。

## 1. 契約差異及決策

- 新增產品內 `/institutional/metrics`；period 僅5／10／20／60，直接使用既有 `InstitutionalCalculator`，不實作2F泛用 `chip-metrics`，不修改計算公式。
- 本次任務指定不支援證券也回404；目前支援 TWSE／TPEX 的 stock。ETF、權證及其他市場不因此取得法人產品支援。
- `trade_scope` 是完整 scope identity，包含 code、三個範圍旗標及 basis；同 code 不同旗標不可合併。省略或只給 code 而存在歧義時422；允許傳入完整嚴格 JSON。未知 code 回200空結果。
- 現有 Phase 1 `trading_calendars` 沒有已核實涵蓋區間、來源 lineage 或版本證據。正式 repository 的 calendar Port 因此回 `verified=false`；不把 weekdays、日行情日期或已匯入法人日期冒充完整交易日曆。累計／連買賣回 `not_verified` 與 null，保留已保存 daily 值。測試以明示 synthetic verified calendar 驗證所有數值及缺日行為。
- TPEx 現有 OpenAPI 缺分類且交易口徑未核實，只存 candidate 時，history空、latest.point=null；Status 揭露 candidate 與能力限制。若未來有合法 partial canonical，quality=partial，來源缺欄為null。
- 無已核實發布 SLA，freshness 僅用 no_data／not_verified，不猜 stale、未發布或更新截止時間。

## 2. Routes

| GET route | Parameters | Response |
| --- | --- | --- |
| `/api/v1/stocks/{id}/institutional` | from/to、trade_scope、limit=250（1–1000）、order=asc/desc、cursor | 完整報表分頁及 lineage／coverage／freshness |
| `/api/v1/stocks/{id}/institutional/latest` | trade_scope | 最新已保存 canonical；沒有則 point=null |
| `/api/v1/stocks/{id}/institutional/metrics` | from/to、trade_scope、category、period（省略回四組） | daily／cumulative／streak、版本、參數及 input_refs |
| `/api/v1/chip-data/status` | product=institutional_daily、market=TWSE/TPEX、source_code、limit=100（1–100）、cursor | 各核實來源的能力、run counters、canonical／candidate 數量 |

`{id}` 是既有 Instrument UUID。日期使用市場本地嚴格 ISO YYYY-MM-DD，inclusive，預設 to=當地今天、from=to−365日；拒絕未來、倒序、超3660日。Metrics 最多讀1000份 canonical，超過拒絕，不截短後聲稱完整。

Category 白名單：foreign_ex_dealers、investment_trust、dealer_proprietary、dealer_hedging、dealer_total、three_major_total。買賣超、dealer與三大總計、累計及連買賣全部委派既有 Calculator。來源回報 subtotal 放 components，重算結果放 derived_daily／metrics，不把 subtotal 與其 leaf 重複相加。

Cursor 是 base64url JSON keyset，含查詢 signature；下一頁必須保留日期、scope、排序、limit等原條件。Signature 用來拒絕錯用 cursor，不是身分驗證或簽章授權。每次請求各自取得 snapshot，不保證跨頁期間 revision 不變。

## 3. Response 與金融語意

實際欄位以 `apps/api/openapi.json` 與 Pydantic schemas 為準。

- History 含 instrument、product、frequency、from/to、items、count、next_cursor、read_at、trade_scope、freshness、coverage、source_contract_versions。
- Point 含 trade_date、完整 trade_scope、quantity_unit、taxonomy_version、九個明確分類 components、quality、derived_daily、timing、provenance。
- Timing 分開 as_of_date、source_published_on、source_published_at、first_seen_at、last_seen_at、ingested_at、timezone_name；timestamp 須 aware，輸出 UTC，統計日期不轉成 UTC 日期。
- Provenance 含 Source id/code/name、revision/run/raw UUID、row locator、parser/contract/promotion versions、normalized hash；不回 raw body、endpoint credentials或內部錯誤。
- Source buy/sell/net 用 signed BIGINT 範圍內十進位整數字串；buy/sell 非負。Derived quantity 用50位整數 Decimal 字串，可超過 BIGINT。拒絕 float、NaN、Infinity、小數數量及超精度值。
- 已確認零為 `"0"`；缺值為null與 unavailable_reasons，缺來源分類不使用其他 subtotal 補值。
- Calculation status 沿用 Domain：complete、missing_data、not_verified、incompatible_data、insufficient_history、truncated_history、no_sessions。每個結果附 formula、algorithm_version、parameters、calendar_version、session/missing dates、input_refs及 reasons。
- 連續買賣在缺少確認交易日或口徑不一致時停止並揭露狀態；未知左邊界回 truncated_history／lower_bound，不能把查詢起點當成完整歷史起點。
- Coverage 的 stored_from/to 只是保存端點。完整度預設 not_verified；verified calendar 發現缺報表時為partial及 known_missing_dates。History 分頁不從單頁推斷全區間缺日。
- Freshness 標示最新保存的統計日、發布時間、last_seen、age_calendar_days；is_realtime固定false。

Status 的 latest_reported_as_of_date 來自已解析 job／accepted item／canonical 的官方統計日，未使用 run 執行日。latest_canonical_date 只計正式報表；candidate_count 計未被 canonical 選中的 distinct candidate revision，不是 raw 或 observation 筆數。latest_source_published_at／last_seen_at 可以包含候選觀察，不因此成為 canonical 金融值。latest_run／last_successful_run 重用專屬 Institutional DataSource 的 IngestionRun；partial 沿用 failed + has_partial_data，counts 保留 accepted、partial、rejected、unresolved等實際 counters，失敗只回安全代碼。

## 4. 架構與一致性

```mermaid
flowchart LR
    A[REST / Pydantic] --> B[InstitutionalQueryService]
    B --> C[InstitutionalReadRepository Port]
    B --> D[Existing InstitutionalCalculator]
    C --> E[SQLAlchemy Query Repository]
    E --> F[Phase 2B-2 Canonical Reader]
    F --> G[(Canonical + selected revision/item)]
    E --> H[market_data.public Reference Reader]
    H --> I[(Instrument / Market / Source / Run)]
    E --> J[Calendar Port]
```

每個 Institutional request 使用一個 SQLAlchemy Session、`REPEATABLE READ READ ONLY` transaction，statement timeout=10秒、lock timeout=3秒。Report、revision、source、freshness、status counters均讀同一 snapshot；多 Worker 在另一 transaction 提交新版不會使單次回應混合版本。讀取不 commit 金融變更、不 autoflush，不加寫入鎖或 advisory lock。

Canonical query 重用既有 `SqlAlchemyInstitutionalRepository.get_canonical`，不直接解析 raw、不晉升 candidate、不變更 Revision Policy。跨模組只透過新增 additive `market_data.public.market_data_reference_reader(session)`；其 Protocol 在 market_data Domain，Infrastructure 由擁有資料的模組實作。沒有 Institutional 私下 import Market/Instrument/Run 私有資料表。

## 5. HTTP Errors

| HTTP | 情境 |
| --- | --- |
| 200 | 正常資料、已存在股票的空歷史／null latest／無可用計算值 |
| 404 | UUID不存在或市場／證券類型不支援 |
| 422 | 日期、limit、category、period、scope、cursor或查詢範圍無效 |
| 503 | DB不可用、canonical資料無法重建、回應契約不合法 |

錯誤 envelope 沿用 detail.code/message/errors。422不回輸入值；503不暴露 SQL、DSN、原始例外或密鑰。GET 不呼叫來源 HTTP，來源匯入失敗不會破壞既有可讀 canonical。

## 6. 執行與驗證

API 使用範例見 [README](../../README.md#法人查詢-api-phase-2b-5)。開發環境原有 Schema 為 head 時無須再升級；需升級的獨立環境先檢查 `alembic current`／`heads`，審查後執行 `alembic upgrade head`。本次沒有新 Migration。

離線測試與 OpenAPI（repo 根目錄，使用已安裝專案的 venv）：

```bash
.tools/api-venv/bin/python -m pytest apps/api/tests/test_institutional_api.py apps/api/tests/test_institutional_query_service.py apps/api/tests/test_institutional_models.py apps/api/tests/test_institutional_calculations.py -q
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
```

PostgreSQL 寫入測試必須用獨立測試 DB，並由既有 fixture 逐例建立／刪除隨機 schema。以下 DATABASE_URL 是**佔位值，必須改成獨立測試 DB 的連線**，不可沿用正式 public：

```bash
cd apps/api
DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/stockapp_test' \
  RUN_INTEGRATION=1 RUN_NETWORK=0 ../../.tools/api-venv/bin/python -m pytest tests/test_institutional_query_postgres.py -q
```

測試涵蓋：真實 PostgreSQL canonical→HTTP、空資料、partial／candidate、不重複日期、scope隔離、超JS安全整數及超BIGINT衍生值、四個累計窗口與Domain比對、連買賣、缺已確認交易日、未核實日曆、來源／contract不一致、修訂與舊版本防護、snapshot重疊提交、正式dependency唯讀隔離、GET無供應商HTTP／raw讀取／DB寫入、status分頁及安全503。所有 fixtures 明示 synthetic，只進隔離 DB／Schema。

2026-11-08 實際驗證結果：

| 檢查 | 指令／執行方式 | 結果 |
| --- | --- | --- |
| 全後端與 PostgreSQL／Redis 回歸 | 在新建臨時 `stockapp_p2b5_test_<random>` DB 執行 `python -m pytest tests -m 'not network' -q --tb=short`；各 DB 案例另用 UUID schema，結束刪除臨時 DB | **1587 passed，3 deselected**，144.08秒 |
| 新 Institutional query PG 測試 | `tests/test_institutional_query_postgres.py`，同樣使用臨時 DB／隔離 schema | **33 passed**；亦包含在上列全套回歸 |
| 新 API／query unit tests | 全套回歸包含 `test_institutional_api.py`／`test_institutional_query_service.py` | **67 passed**，含1000筆輸入上限、數值、HTTP與精度檢查 |
| Vitest | `PATH="$PWD/.tools/node/bin:$PATH" npm test` | **244 passed，15 files** |
| TypeScript | 同一 Node PATH，`npm run typecheck` | 通過 |
| Next.js Production Build | 同一 Node PATH，`npm run build` | 通過，還原build產生的next-env檔，沒有前端程式變更 |
| OpenAPI | `python apps/api/scripts/export_openapi.py --check`，另於新 Backend container 執行相同check | 通過 |
| Docker | `docker compose config --quiet`、`docker compose build backend`、`docker compose up -d --no-deps --wait --wait-timeout 120 backend` | 成功；backend／frontend／postgres／redis均healthy |
| Migration read-only inspection | Alembic ScriptDirectory head 比對 `SELECT version_num FROM alembic_version`，container `alembic current` | PostgreSQL **17.11**，current=head=`0006_institutional_jobs`，沒有執行正式upgrade或Schema變更 |
| 實際 HTTP | `/health`、`/health/ready`、2330四支法人查詢／status、既有日行情latest、首頁與個股頁 | 全部200；runtime OpenAPI含四條新路由；2330既有行情最新日2026-11-05 |
| Playwright | `PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers" npm run test:browser --workspace @stockapp/web -- e2e/stock-chart.spec.ts --grep 'search 2330 and inspect real|mobile name search'`（同Node PATH） | **2 passed**，桌面真實日／週／月圖與手機搜尋／尺寸調整 |
| 邊界及diff | AST檢查Domain/Application無framework或Infrastructure依賴、Institutional不直接import market_data私有Infra；`git diff --check` | 通過 |

全套Pytest有1個既有 Starlette/httpx TestClient deprecation warning，沒有為此升級依賴。3個 network tests 由 `-m 'not network'` 明確排除，沒有對外下載或真實匯入驗證。正式canonical_count=0；實際History count=0、Latest point=null、Metrics calendar.verified=false且value=null/status=not_verified，與資料狀態一致。非空PostgreSQL canonical及各數值由明示synthetic隔離測試驗證，不宣稱正式環境已有法人歷史。

只重建／替換Backend；既有history-worker維持原本exited狀態，沒有重啟或改動其程式。沒有commit、push或開始2B-6。

## 7. 修改檔案

- `apps/api/src/stockapp/main.py`：註冊路由與安全錯誤處理。
- `modules/institutional/api/{__init__,dependencies,routes,schemas}.py`：四支GET、Pydantic契約與唯讀snapshot。
- `modules/institutional/domain/queries.py`：純Domain read Port及查詢上限／錯誤。
- `modules/institutional/application/query_service.py`：scope／分頁／coverage／Calculator組裝。
- `modules/institutional/infrastructure/query_repository.py`：canonical及Institutional audit讀取。
- `modules/market_data/public.py`、`domain/references.py`、`infrastructure/reference_reader.py`：跨模組reference公開邊界（上述modules路徑均位於`apps/api/src/stockapp/`）。
- `apps/api/tests/test_institutional_{api,query_service,query_postgres}.py`：離線、數值與真實PostgreSQL測試。
- `apps/api/openapi.json`：runtime產生的snapshot。
- `README.md`、`docs/specs/phase-2-api-contract.md`、`docs/specs/phase-2-roadmap.md`及本規格：指令、差異、決策與驗證。

原先已存在的`docs/operations/institutional-migration-2026-11-08.md`未提交修改予以保留，本次沒有編輯該檔。

## 8. 尚需前置條件

- TWSE 自動下載使用權仍 pending，不因建立 REST API 而啟用。
- TPEx 分類及 scope 缺口仍在，候選觀察不轉為完整 canonical。
- 正式累計／連買賣數值需要具版本、來源證據及涵蓋區間的 verified Trading Calendar Port；本次未擴充核心 Schema。
- 正式 canonical 尚空時 API 正確回空；本次未對正式 DB 寫入 synthetic 或新抓行情。
- 法人 UI、Phase 2B-6 及其他籌碼產品均未開始。
