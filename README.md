# Stock Analysis Platform

台灣上市／上櫃股票分析平台。採 Monorepo、Modular Monolith + Data Workers；目前包含股票搜尋、日／週／月 K 線、技術指標、個股籌碼 Dashboard、官方資料匯入 CLI 與 REST API。行情與籌碼僅讀 PostgreSQL 已保存資料，非即時報價；沒有回測、選股器或即時交易功能。

## 目錄與邊界

```text
apps/
  web/                    Next.js App Router、TypeScript、Tailwind CSS、Vitest
  api/
    src/stockapp/
      modules/health/     api → application → domain；infrastructure 實作 domain ports
      modules/market_data/ 純 Python Domain、Repository Ports、PostgreSQL mappings
        api/             股票搜尋、行情與資料狀態 REST API
        application/     日行情正規化、驗證與匯入服務
      modules/technical/ Decimal 指標引擎、歷史輸入 Port、Application 與版本化 API
      modules/institutional/ 法人 Domain、typed revisions、audit 與 PostgreSQL repositories
      modules/ownership/   TDCC週快照、版本化級距、raw／audit／revision及手動CLI
      modules/margin/      融資／融券雙Ledger、raw／audit／revision、匯入CLI與唯讀API
      modules/lending/     借券雙Ledger、Source／Channel Metadata、Raw／Revision、CLI與API
      modules/chip_analysis/ 版本化Engine、跨來源Canonical API、時間可用性與公開Ports
      shared/domain/     資料來源 DataSource Protocol
      shared/infrastructure/ SQLAlchemy Base 與 engine factory
      config.py          環境設定
      main.py            FastAPI composition root
      cli.py             官方同步、archive 重播、歷史工作與匯入狀態查詢
    tests/               Pytest 單元與真實服務整合測試
    migrations/          Alembic schema revisions（啟動時不自動執行）
    requirements.lock    含雜湊的 runtime／test 依賴鎖檔
    openapi.json         與 runtime 比對的 API contract snapshot
workers/                 手動歷史回補 worker 操作文件；預設不啟動
packages/contracts/      API 合約／未來產生的 client
docs/architecture/       架構決策
docs/specs/              範圍與驗收
compose.yaml             PostgreSQL、Redis、API、Web
```

資料供應商須透過 Domain 定義的 Adapter 介面接入；API 不直接呼叫外部行情來源。API 與 worker 未來共用 Python domain/application，無需先拆成微服務。Phase 1A 建立六個核心模型與獨立標準化 revision 表，未新增股票 API 或 UI；完整 schema、去重規則與安全回退方式見 [Phase 1A 規格](docs/specs/phase-1a-database.md)。

Phase 1B 的管線、逐筆異常與重試規則見 [匯入規格](docs/specs/phase-1b-ingestion.md)，實際端點、欄位、授權與更新頻率見 [官方來源查證](docs/specs/phase-1b-sources.md)。

Phase 1C 的 API、聚合／完整度、查詢上限與錯誤碼見 [股票查詢 API 規格](docs/specs/phase-1c-market-api.md)。此階段未修改核心資料模型，也沒有新 Migration。

Phase 1D 的前端架構、圖表語意及驗收見 [股票圖表前端規格](docs/specs/phase-1d-stock-chart.md)。沿用 Next.js App Router，使用固定版本 Lightweight Charts 5.2.1；沒有改動後端核心模型或抓取新行情。

Phase 1E 的公式、初始化、缺值、精度及 API 合約見 [技術指標規格](docs/specs/phase-1e-technical-indicators.md)。所有功能共用 technical Domain 引擎，沒有新增套件、Migration 或供應商請求。

Phase 2B-1 新增獨立的 institutional 純 Domain，提供法人分類／完整度契約、5／10／20／60 確認交易日累計及連續買賣超計算，該子階段僅含純 Domain，後續整合見2B-2至2B-5。數值、缺失與來源口徑規則，以及不需 Docker 的測試指令見 [法人 Domain 規格](docs/specs/phase-2b-institutional-domain.md)。

Phase 2B-2 新增法人專用 source／raw／item audit、不可變 report revisions、原子 canonical 指標與 `0005_institutional_persistence` Migration，並提供 `market_data.public` 的 Instrument Identity Reader。完整 Schema、冪等／並發與晉升政策、獨立測試資料庫指令及非空 downgrade 保護見 [法人 Persistence 規格](docs/specs/phase-2b-persistence.md)。回補中的環境請另行安排升級時機；本機後續依授權完成的升級見下方紀錄。

Phase 2B-3 新增 TWSE／TPEx 法人 Adapter、版本化 Source Manifest、Parser 與 Normalizer，固定 Fixture／Mock HTTP 測試不接觸正式資料庫。下載預設關閉；TWSE 權限待確認，且只解析兩個已稽核日期，TPEx 為 latest-only 且自營商明細不完整。能力、使用方式、驗證及後續限制見 [法人 Adapter 規格](docs/specs/phase-2b-adapters.md)。該子階段未建立CLI或API；後續分別在2B-4／2B-5加入。

Phase 2B-4 加入手動法人匯入 CLI、raw-first pipeline、逐列 audit／transaction 與可續跑 checkpoint，詳見 [法人匯入規格](docs/specs/phase-2b-ingestion.md)。本機 `stockapp/public` 已於2026-11-08依明確授權由0004升級至 `0006_institutional_jobs`，Schema及現有網站驗證通過，見[升級紀錄](docs/operations/institutional-migration-2026-11-08.md)。TWSE 停用、TPEx 僅最新 partial 能力；Migration沒有新增法人 API／UI或匯入法人資料。

Phase 2B-5 新增法人 History／Latest／Metrics／Source Status REST API，讀取既有 PostgreSQL canonical，直接重用 Domain Calculator；完整契約、snapshot 與交易日曆限制見 [法人 API 規格](docs/specs/phase-2b-api.md)。沒有新 Migration 或法人 UI；TWSE 使用權、TPEx partial／candidate 限制繼續保留。

Phase 2B-6 已完成軟體端到端與回歸驗收；已核准TPEx真實raw／audit驗證通過，但TWSE使用權、TPEx交易口徑／dated identity及verified日曆仍使真實canonical→API驗收Blocked。正式法人資料仍為0，沒有以Fixture補齊。詳見 [完整驗收報告與重跑指令](docs/specs/phase-2b-validation.md)。

Phase 2D-1 新增 margin 純 Domain：融資／融券分開的 balance identity、Signed Adjustment 版本政策、嚴格缺值狀態、限額生效日、經證券與有效期間核實的張股換算，以及 M1／M2／M5 Source Capability Manifest。沒有新增資料表、下載、匯入、API 或畫面；TWSE 無日期與 TPEx latest-only 限制維持明確。公式、來源 gate、固定數值測試指令與 2D-2 待辦見 [融資融券 Domain 規格](docs/specs/phase-2d-margin-domain.md)。

Phase 2D-2 完成 Margin Persistence／手動匯入／REST API，新增 `0008_margin` Migration。每份雙 Ledger 報表原子晉升、來源修訂保留、Raw／audit 可追溯；查詢使用一致唯讀 Snapshot。完整 Schema、來源限制、Evidence 格式與實際驗證結果見 [融資融券管線與 API 規格](docs/specs/phase-2d-margin-api.md)。

Phase 2E-1 新增獨立 lending 純 Domain，分開借券未還、借券賣出流量／餘額與 Regulatory Quota。提供精確 balance identity、帶正負號調整、經核實分割證據的 Channel 合計／對帳，以及 M3／M4／M6／M7／M8／M9 Source Manifest；不把 SLBNLB 再加到子系列、不猜未知日期／歷史／Channel。公式與不需資料庫的測試指令見 [借券 Domain 規格](docs/specs/phase-2e-lending-domain.md)。本階段沒有新增表、Migration、下載、CLI、API 或前端。

Phase 2E-2 新增 `0009_lending`、不可變Raw／Audit／Source-Channel-Kind Metadata／Revision、原子Canonical與手動Ingestion／Replay／查詢API。目前只核准TPEx M6借券賣出，M4借券未還的使用權／Schema仍Blocked；不同kind／Channel／scope分開，GET不抓來源。完整Schema、來源限制、操作與實際驗證見 [借券管線與API規格](docs/specs/phase-2e-lending-api.md)。

Phase 2F-1 新增 `chip_analysis` 共用純分析引擎：法人累計／連買連賣、集保比例／百分點變化、Margin餘額差、Lending餘額／日差／流量及有明確相容性證據的借券賣出比例。直接重用各模組Calculator，保留Input Revision、知識截止時間與50位Decimal／12位HALF_EVEN輸出；缺值不補0、未來發布資料不使用。呼叫端提供Canonical Domain Snapshot或注入公開Reader Port，本階段沒有SQL reader wiring、API、UI或Migration。契約與數值見 [籌碼分析引擎規格](docs/specs/phase-2f-chip-engine.md)。

```bash
# 純Domain固定數值測試，不連資料庫或供應商
.tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_chip_analysis_contracts.py \
  apps/api/tests/test_chip_analysis_products.py \
  apps/api/tests/test_chip_analysis_ledgers.py -q
```

## Phase 2整合驗收（Phase 2G-2）

完整功能、真實資料覆蓋、來源Partial／Blocked、隔離PostgreSQL／Migration回歸、正式資料保護、API／瀏覽器與實測效能，見 [Phase 2最終驗收報告](docs/specs/phase-2-final-validation.md)。軟體測試通過不代表已取得完整法人、借券未還或十年籌碼資料；正式來源缺口與公開發布前置條件保留在報告中。本階段不新增功能、不部署或進入Phase 3。

## 個股籌碼 Dashboard（Phase 2G-1）

在既有 `/stocks/<instrument UUID>` 個股頁，價格 K 線與技術指標下方可切換「三大法人／集保持股／融資融券／借券賣出」。支援近1個月、近3個月、近1年與自訂區間（最多366個日曆日）；籌碼區間獨立於價格圖表，不會重設既有技術指標。已造訪的 Tab 保留圖表、選擇及縮放視窗，同一區間切回不重複載入。

- 法人：每日外資／投信／自營商買賣超、後端5／10／20／60交易日累計及連買連賣，並以相同日期區間的獨立收盤價圖對照。
- 集保：原生週統計、大戶比例與百分點週變化、各級距持有人數／股數／官方比例。門檻為官方可精確表示的400,001股或1,000,001股起，不估算切穿級距的門檻。
- 融資融券：兩個Ledger分圖，呈現餘額及後端計算的每日增減。
- 借券：借券賣出與借券未還分開選擇；餘額與每日流量分圖，多個Channel須選擇單一序列，來源範圍未核實時保留提示。

每個Tab顯示統計日、來源、發布／首次觀察／匯入時間、完整度與Revision。空資料為Unavailable；缺值留白，週資料不日化。來源未核實發布SLA時顯示「時效未核實」，只有API明確回傳延遲狀態才標示Stale。金融數值以字串保留精度，前端不重算金融公式。

瀏覽器只呼叫既有 `/api/market/...` 同源Proxy，不連TWSE／TPEx／TDCC。現有後端及資料庫即可使用，這次沒有Migration或新環境變數，也不會因開啟畫面自動匯入資料。開發驗證：

```bash
npm ci
# 第一個終端；BACKEND_URL僅供Next伺服器使用
BACKEND_URL=http://localhost:8000 npm run dev --workspace @stockapp/web -- --port 3100
# Windows瀏覽器開啟 http://localhost:3100，搜尋股票進入個股頁
# 第二個終端
npm test
npm run typecheck
npm exec --workspace @stockapp/web -- playwright install chromium
BROWSER_BASE_URL=http://localhost:3100 npm run test:browser --workspace @stockapp/web -- chip-dashboard.spec.ts
# 結束dev server後執行，避免同時寫入.next
npm run build
```

Dashboard E2E使用明確標記SYNTHETIC的同源API攔截資料，只驗證畫面行為，不代表正式資料覆蓋率。本機真實資料與來源限制、測試結果見 [籌碼 Dashboard 規格](docs/specs/phase-2g-chip-dashboard.md)。目前法人Canonical為空、集保只有一週、融資／借券各只有一檔的已驗證觀察；缺週變化、完整累計與尚未核准的借券未還功能維持Unavailable，不宣稱十年籌碼資料完整。

## 跨來源籌碼 API（Phase 2F-2）

已新增 `GET /api/v1/stocks/{id}/chip-metrics`，使用同一個 PostgreSQL RR READ ONLY snapshot，透過各模組公開 Reader 讀取 Canonical，直接重用2F-1 Engine。支援重複的 `metric`、原生週資料與每日可用時間對齊、參數、來源／Revision／完整度、日期分頁；GET不抓供應商。金融數值與Metadata中的BIGINT為精確字串。

```bash
# 查看已保存的真實融資／借券與其他來源缺口；明確使用查詢時已知資料重算歷史
curl -G 'http://localhost:8000/api/v1/stocks/fe53c1d3-e0ca-4475-8ab6-d2c274252e25/chip-metrics' \
  --data-urlencode 'metric=margin_balance_change' \
  --data-urlencode 'metric=lending_balance' \
  --data-urlencode 'metric=institutional_net_sum' \
  --data-urlencode 'from=2026-11-06' --data-urlencode 'to=2026-11-09' \
  --data-urlencode 'time_basis=query_as_of'

# TDCC週資料按每日當時已知的時間對齊；原始統計日與age仍保留
curl -G 'http://localhost:8000/api/v1/stocks/6afb0bb3-9356-4a59-8122-57fed2b36628/chip-metrics' \
  --data-urlencode 'metric=ownership_large_holder_percent' \
  --data-urlencode 'evaluation_frequency=1d' \
  --data-urlencode 'from=2026-11-06' --data-urlencode 'to=2026-11-11'
```

預設 `time_basis=as_of_day`，未公布／未觀察／未ingest的資料不得提早出現；歷史回補可能回not_yet_available。只有明確選 `time_basis=query_as_of` 才用查詢時已知Canonical重算過去統計，兩者都明示 `point_in_time_supported=false`。週資料不偽造成每日新持股，缺值不補0。

每頁1–250個評估日期、最多1000個metric points；區間最多3660日，參數JSON最多4096字元，單產品輸入最多1000份。`limit`計日期，`count`計metric points；下一頁使用`next_cursor`保留原條件與截止時間。多metric參數用`{"metric_id":{"parameter":value}}`，詳細範例見[跨來源API規格](docs/specs/phase-2f-chip-api.md)。

本機真實驗收：2330／2026-11-06、大戶門檻400001股的集保存量比例 **87.009580453709%**；6488／同日融資餘額日變化 **−1,128,000股**。6488／2026-11-09的借券Canonical可追溯，但Channel／scope未核實，因此分析結果維持not_verified／null。法人Canonical仍為0，沒有以Synthetic資料補足。沒有新增Migration、UI或排程；正式行情與籌碼筆數未變。

## 借券資料匯入與 API（Phase 2E-2）

```bash
docker compose build backend
docker compose run --rm backend alembic current
docker compose run --rm backend alembic upgrade head
docker compose up -d --no-deps --wait backend

# 預設已核准的M6官方最新快照；缺reviewed UUID/date證據時Raw保存、股票列unresolved
docker compose run --rm backend stockapp lending sync latest --source M6
docker compose run --rm backend stockapp lending status --limit 20

# 替換經審查的實際證據路徑；M6原單位為shares，不猜張股係數
# docker compose run --rm -v /absolute/reviewed-lending.json:/evidence.json:ro backend \
#   stockapp lending sync latest --source M6 --evidence /evidence.json

docker compose run --rm backend stockapp lending replay --raw <RAW_UUID>
docker compose run --rm backend stockapp lending replay --resume <RUN_UUID>

# M6 latest-only會拒絕，不能拿最新資料冒充指定日期
docker compose run --rm backend stockapp lending sync date --source M6 --date 2026-11-06
```

Source ID沿用2A的M3／M4／M6／M7／M8／M9；只有M6啟用手動下載，其餘維持使用權／Schema／產品能力阻擋。Replay不連外，Resume使用原工作凍結Evidence。Partial／failed退出碼2可用status查原因。

```bash
curl 'http://localhost:8000/api/v1/stocks/<INSTRUMENT_UUID>/lending/latest'
curl 'http://localhost:8000/api/v1/stocks/<INSTRUMENT_UUID>/lending?report_kind=lending_short_sales&from=2026-01-01&to=2026-11-10&limit=100'
curl 'http://localhost:8000/api/v1/stocks/<INSTRUMENT_UUID>/lending?report_kind=borrowed_positions'
curl 'http://localhost:8000/api/v1/lending/status?source_code=tpex_lending_short_openapi'
curl 'http://localhost:8000/api/v1/chip-data/status?product=lending_daily'
```

History回傳各Channel獨立point，不加總每日balance或不同kind；Latest若選到多個Channel回422要求篩選。Quantity／Decimal使用精確字串，Quota與實際借券／賣出流量分開。M6目前保留unknown Channel、trade_scope=unspecified與未知發布時間，品質／歷史完整度不假稱verified；沒有借券未還canonical時回空資料。

目前本機 Schema 已升級至 `0009_lending`，後端完整回歸 **2,756 passed／4 network deselected**。已保存官方 M6 **2026-11-09** 的一份 Raw（933 筆資料列）及逐列 Audit，並以核實的單日身分證據完成 **環球晶（6488）一檔、一個交易日**的 Canonical／資料庫／API 逐欄驗收。借券賣出餘額為 **12,464,000 股**，重播後仍為 **1 份 Canonical／1 個 Revision**；其餘 932 筆維持 unresolved，不宣稱全市場或歷史完整。既有行情與籌碼資料筆數未變；完整實測見上述規格。

```bash
# 本機已保存的真實 M6 報表；此 Phase 沒有新增前端畫面
curl 'http://localhost:8000/api/v1/stocks/fe53c1d3-e0ca-4475-8ab6-d2c274252e25/lending/latest'
curl 'http://localhost:8000/api/v1/stocks/fe53c1d3-e0ca-4475-8ab6-d2c274252e25/lending?from=2026-11-09&to=2026-11-09'
```

## 融資融券匯入與 API（Phase 2D-2）

```bash
docker compose build backend
docker compose run --rm backend alembic current
docker compose run --rm backend alembic upgrade head
docker compose up -d --no-deps --wait backend

# TPEx M5 只支援官方最新快照。未附審查證據時，保留 Raw／Audit 並將身分／換算列為 unresolved。
docker compose run --rm backend stockapp margin sync latest --market tpex
docker compose run --rm backend stockapp margin status --limit 20

# 替換成已審查的絕對路徑，證據須與實際官方日期／商品相符；不可假定所有商品一張1,000股。
# docker compose run --rm -v /absolute/reviewed-margin.json:/evidence.json:ro backend \
#   stockapp margin sync latest --market tpex --evidence /evidence.json

docker compose run --rm backend stockapp margin replay --raw <RAW_UUID>
docker compose run --rm backend stockapp margin replay --resume <RUN_UUID>

# M1／M5 沒有歷史日期參數：這個命令會明確拒絕，不會下載日期相近資料冒充。
docker compose run --rm backend stockapp margin sync date --market tpex --date 2026-11-06
```

TWSE M1 可保存最新 Raw，但沒有官方 Date，缺少同份 Payload 的可信日期證據時禁止 canonical；M2 dated 網站來源使用權／群組 Schema 未核實，保持停用。沒有十年融資融券覆蓋。CLI 的 Partial／failed 退出碼為 2，可用 status 查原因；Replay 不連外，Resume 使用原工作凍結的 Evidence。

```bash
curl 'http://localhost:8000/api/v1/stocks/<INSTRUMENT_UUID>/margin/latest'
curl 'http://localhost:8000/api/v1/stocks/<INSTRUMENT_UUID>/margin?from=2026-01-01&to=2026-11-09&limit=100'
curl 'http://localhost:8000/api/v1/margin/status?market=TPEX'
curl 'http://localhost:8000/api/v1/chip-data/status?product=margin_daily'
```

融資、融券各自回傳餘額、買進／賣出、償還與每日變化；Quantity 包含精確字串 value、unit 和 availability。缺值維持 null，來源 lots 與經審查換算後 shares／units 分開。API 保留 Revision、來源及統計／發布／擷取時間，不在 GET 抓資料、不宣稱即時或完整歷史。尚無 canonical 的股票回傳空資料，Partial candidate 仍可由 status 查詢。

Phase 2D-2 驗收時 Schema 為 `0008_margin`（目前已升級至 `0009_lending`），實際保存 **6488／2026-11-06 一檔、一個交易日**；再次同步後仍為1份Canonical／1個Revision。融資餘額10,285,000股、融券餘額317,000股，已逐欄比對官方Raw、PostgreSQL與HTTP API。其餘918筆M5證券因身分／乘數未核實維持unresolved；TWSE M1因無日期仍無Canonical，不宣稱歷史完整。單日審查證據與實測紀錄見規格。

```bash
curl 'http://localhost:8000/api/v1/stocks/fe53c1d3-e0ca-4475-8ab6-d2c274252e25/margin/latest'
```

```bash
# 建立獨立臨時DB執行完整離線驗收，結束只清除測試DB
.tools/api-venv/bin/python apps/api/scripts/run_institutional_validation.py
# 明確啟用已核准TPEx最新資料的隔離驗證；不啟用TWSE
.tools/api-venv/bin/python apps/api/scripts/run_institutional_validation.py --live-tpex
```

## 集保週資料匯入（Phase 2C-1）

新增ownership模組、TDCC官方CSV管線與`0007_ownership` Migration。完整公式、Schema、身分證據與驗證見 [集保匯入規格](docs/specs/phase-2c-ownership-ingestion.md)。目前實際canonical僅 **2330、2026-11-06一週**；完整Raw有4,115組，其餘因身分證據未核實維持unresolved，不宣稱十年歷史。

```bash
# 本機已升至0007；其他環境先檢查再執行必要升級
docker compose build backend
docker compose up -d --no-deps --wait backend
docker compose exec backend alembic current
# docker compose exec backend alembic upgrade head

# 最新來源；缺少reviewed evidence時保留raw／audit，不猜市場UUID
docker compose run --rm backend stockapp ownership sync latest
docker compose run --rm backend stockapp ownership status --limit 20
# 使用已審查的host evidence檔（替換絕對路徑）
# docker compose run --rm -v /absolute/reviewed-evidence.json:/evidence.json:ro backend \
#   stockapp ownership sync latest --identity-evidence /evidence.json

docker compose run --rm backend stockapp ownership replay --raw <RAW_UUID>
docker compose run --rm backend stockapp ownership replay --resume <RUN_UUID>
```

Host安裝版可用`.tools/api-venv/bin/stockapp ownership ...`。Evidence格式見規格；本機已核對且僅適用2026-11-06的檔案在`.tools/validation/phase-2c1/identity-evidence.json`，不可當成其他週的通用證據。`sync date`不支援；replay不連外。CLI退出碼2可表示partial：2330成功保存，但其他4,114組仍unresolved。Regular／adjustment／total分開，百分比保留來源值。該子階段沒有新增集保REST API或前端；API已在2C-2加入。

## 集保查詢與大戶比例 API（Phase 2C-2）

可查已保存的Latest／Weekly History／Distribution／大戶比例與來源狀態，完整契約與公式見 [集保 API 規格](docs/specs/phase-2c-ownership-api.md)。目前只有2330／2026-11-06一週，比例可計算，週變化因缺前一週與verified schedule回null。

```bash
INSTRUMENT_ID="$(curl --fail --silent 'http://localhost:8000/api/v1/instruments?symbol=2330&market=TWSE&instrument_type=stock' | python3 -c 'import json,sys; print(next(x["id"] for x in json.load(sys.stdin)["items"] if x["symbol"]=="2330"))')"
curl --fail "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/ownership/latest"
curl --fail --get "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/ownership" \
  --data-urlencode 'from=2026-01-01' --data-urlencode 'to=2026-11-06' --data-urlencode 'limit=250'
curl --fail "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/ownership/distribution?as_of=2026-11-06"
curl --fail --get "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/ownership/metrics" \
  --data-urlencode 'from=2026-01-01' --data-urlencode 'to=2026-11-06' \
  --data-urlencode 'threshold=400001' --data-urlencode 'threshold_unit=shares'
curl --fail 'http://localhost:8000/api/v1/chip-data/status?product=ownership_weekly'
```

`threshold`是inclusive基礎股數／受益單位，沒有張數自動轉換。400,000股／1,000,000股切穿級距會回unsupported，不估算；400,001股／1,000,001股可精確計算。Financial值是字串，週變化單位是percentage_points，缺失為null。所有GET只讀PostgreSQL canonical，不在請求時抓TDCC；status是全市場dataset，不支援market篩選。本次沒有新Migration或前端畫面，可在 <http://localhost:8000/docs> 查看契約。

## 法人查詢 API（Phase 2B-5）

先確認目前 Backend 映像已包含新路由。已有 PostgreSQL／Redis 的環境可以只更新 Backend：

```bash
docker compose build backend
docker compose up -d --no-deps --wait backend
docker compose exec backend alembic current
docker compose exec backend alembic heads
# 僅在缺少既有 Migration 且已審查時執行；2B-5本身沒有新Migration
# docker compose exec backend alembic upgrade head
curl --fail http://localhost:8000/health/ready
```

從已保存主檔搜尋2330並取得 UUID（需要 curl、python3）；日期參數換成要查詢的市場本地日期，以下不會觸發下載：

```bash
INSTRUMENT_ID="$(curl --fail --silent 'http://localhost:8000/api/v1/instruments?symbol=2330&market=TWSE&instrument_type=stock' | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')"

curl --fail --get "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/institutional" \
  --data-urlencode 'from=2026-01-01' --data-urlencode 'to=2026-11-06' \
  --data-urlencode 'limit=250' --data-urlencode 'order=asc'
curl --fail "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/institutional/latest"
curl --fail --get "http://localhost:8000/api/v1/stocks/$INSTRUMENT_ID/institutional/metrics" \
  --data-urlencode 'from=2026-01-01' --data-urlencode 'to=2026-11-06' \
  --data-urlencode 'category=foreign_ex_dealers' --data-urlencode 'period=20'
curl --fail 'http://localhost:8000/api/v1/chip-data/status?product=institutional_daily'
```

`period` 可選5／10／20／60，省略回四組；category也支援 investment_trust、dealer_proprietary、dealer_hedging、dealer_total、three_major_total。history最大1000份／頁，日期區間最多3660日；metrics最多1000份輸入。下一頁沿用原參數並傳 next_cursor。多個scope不能自動混用，需指定 trade_scope；完整JSON篩選方式見規格。

所有新法人數量／計算值以字串輸出；null與`"0"`不同。已有股票但沒有canonical時history回200空、latest.point=null。TPEx未核實scope的candidate只在status揭露，缺自營商分類不填0。現有日曆沒有核實coverage／版本，所以正式累計與連買賣保守回not_verified／null；不以工作日猜測。API不宣稱即時或完整歷史，資料只來自已保存canonical。

可在 <http://localhost:8000/docs> 查看四支 API；本次沒有新增法人前端畫面。測試與隔離 PostgreSQL 指令見 [法人 API 驗證](docs/specs/phase-2b-api.md#6-執行與驗證)。

## 法人手動匯入（Phase 2B-4）

以下使用已安裝的 `stockapp`（安裝方式見本機開發）。先將 `DATABASE_URL` 指向核准的獨立測試資料庫並套用 `0006_institutional_jobs`；不要直接對回補中的正式資料庫執行 Migration。本機專案環境可將 `stockapp` 替換成 `.tools/api-venv/bin/stockapp`。

```bash
# 一次手動同步核准的 TPEx 最新資料；不指定或推測官方交易日
stockapp institutional sync latest --market tpex
stockapp institutional sync latest --market tpex --identity-evidence /path/to/reviewed-evidence.json

# 語法支援日期要求；目前會明確拒絕（TPEx latest-only、TWSE使用權pending）
stockapp institutional sync date --market tpex --date 2026-11-06
stockapp institutional sync date --market twse --date 2026-11-06

stockapp institutional status --limit 20
stockapp institutional status --id <RUN_UUID>

# 用status的selected_raw_id重播：建立新run，保留原fetched_at，不下載
stockapp institutional replay --raw <RAW_UUID>
# 續跑尚未結束的checkpoint，沿用凍結版本與身分證據，不下載
stockapp institutional replay --resume <RUN_UUID>
```

沒有已核實 dated Instrument UUID 證據時，真實資料會保留 raw 並標 unresolved；evidence 檔格式見規格。TPEx 缺完整自營商分類且 scope 未確認，即使成功保存報表也只成為 candidate，不能宣稱完整 canonical。status 區分 succeeded／failed／partial／running，並列 accepted、partial、rejected、unresolved 與 promotion 數量；退出碼0／2／1分別表示成功、資料或能力未完整、基礎設施錯誤。沒有建立排程，也不會啟動 history-worker。

## Docker 啟動（建議）

需求：Docker Engine／Docker Desktop 正常啟動、Docker Compose v2 以上（支援 `up --wait`）。WSL 必須在 Docker Desktop 開啟此 distro 的 WSL integration。

以下指令皆從本 README 所在的 repository 根目錄執行：

```bash
test -f .env || cp .env.example .env
# .env 使用本機開發佔位密碼；請勿用於公開部署，也不要提交 .env。
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic check
docker compose ps
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
```

開啟 <http://localhost:3000>。API 文件：<http://localhost:8000/docs>。

使用 WSL Ubuntu 時，請在 Windows 的 Chrome／Edge 開啟以上網址。若 Docker 不可用，在 Docker Desktop 的 Settings → Resources → WSL Integration 啟用使用中的 Ubuntu，並以 `docker version` 確認 Client 與 Server 都可連線。若 Compose 回報 port 已占用，先在原本的開發終端按 Ctrl+C 停止本機 Next.js／Uvicorn，再重新啟動 Compose。

- `/health`：API 存活時回傳 HTTP 200、`{"status":"ok","service":"stockapp-api"}`，不要求資料庫在線。
- `/health/ready`：實際執行 PostgreSQL `SELECT 1` 與 Redis `PING`，全部成功回傳 HTTP 200；失敗回傳 HTTP 503，`status=degraded` 及各服務狀態，不揭露連線字串。
- 首頁由 Next.js server 向後端讀取 readiness，重新整理更新狀態；瀏覽器不需要存取 Docker 內部 hostname，也不需要 CORS。
- 股票查詢使用同源 `/api/market/*`，由 Next.js GET proxy 轉至 FastAPI `/api/v1/*`。`BACKEND_URL` 只存在伺服器；前端不向 TWSE／TPEx 發出請求。
- Compose 依健康檢查順序啟動相依服務；所有公開 port 預設只綁定 localhost。

直接驗證資料庫及 Redis：

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec redis redis-cli ping
docker compose logs --tail=100 backend frontend
```

停止服務（保留資料）：

```bash
docker compose down
```

只有確定要**刪除本機資料**時才執行 `docker compose down -v`。PostgreSQL 初始化設定只在空 volume 生效，已有資料後改密碼須同步修改資料庫角色。

## 官方日行情匯入（Phase 1B）

先執行前面的 Compose 啟動與 Migration 指令，再從 repository 根目錄執行：

```bash
# 上市／上櫃各下載官方當前快照與公司名錄
docker compose run --rm backend stockapp sync latest --market all
# 或指定單一市場：--market twse / --market tpex

# 包含 ETF、ETN、權證、特別股、TDR、REIT 等的最新全證券快照
docker compose run --rm backend stockapp sync latest --market all --scope securities

# 檢查匯入數量、錯誤、來源端點、原始回應時間與 SHA-256
docker compose run --rm backend stockapp runs --limit 5
docker compose run --rm backend stockapp runs --id <RUN_UUID>

# 重新處理已保存的官方 raw；日期必須有對應 archive
docker compose run --rm backend stockapp backfill --market twse --date 2026-11-03
docker compose run --rm backend stockapp backfill --market tpex --date 2026-11-04
# 全證券 archive 與股票 archive 分開選取；不會連網補抓歷史
docker compose run --rm backend stockapp backfill --market twse --scope securities --date 2026-11-04
```

`latest` 是來源提供的資料日期，不保證是執行當天。**backfill 僅支援資料庫已保存的官方原始回應，不提供未確認的遠端歷史下載**；可重複 `--date` 指定多天，無 archive 時明確失敗，絕不替換最新行情的日期。

退出碼 0 代表成功；2 代表 partial／資料失敗。遇到個股缺價或名錄有股票但行情缺列時，run 會標 failed + Partial coverage，但有效股票仍會保留；請讀 accepted／rejected／missing 與逐筆原因。重送不產生重複 canonical bar。1 代表設定／DB 等操作錯誤。

原始資料提供：臺灣證券交易所（上市個股日成交資訊／上市公司基本資料）、證券櫃檯買賣中心（上櫃股票行情／上櫃公司基本資料）。依[政府資料開放授權條款第1版](https://data.gov.tw/license)利用；本平台另做股票範圍篩選、標準化與驗證，不代表交易所背書。單位與限流查證見來源規格，預設5秒間距是專案的保守設定。

`--scope securities` 使用官方 ISIN 名錄分類，TWSE 改用官方每日收盤行情「全部」匯出，TPEx 使用既有全證券行情 OpenAPI。各證券保存 `quote_currency`（例如 TWD／USD／CNY）及 `quantity_unit`（shares／units）；沿用的 `volume_shares` 欄位保存基礎股數／受益或權證單位，**不是張數**。缺 OHLC 不補價，無成交列只保留證券 metadata、raw 及處理紀錄。名錄有但來源沒有日 OHLCV 的證券僅建 metadata 並列 missing；不宣稱債券、興櫃、全部歷史或所有金融商品均已涵蓋。

全證券組合來源含網站匯出與證券名錄，其完整組合的 OGDL 授權未確認，CLI 的 `license` 為 null 並附來源使用條款，不能套用上段股票 OpenData 授權。詳細端點、分類、外幣佐證、重播與維護限制見[全證券匯入規格](docs/specs/taiwan-securities-ingestion.md)。目前網站搜尋／圖表仍為股票介面；新增種類可透過 `/api/v1/instruments` 查詢 metadata，不會冒充普通股。

```bash
curl 'http://localhost:8000/api/v1/instruments?symbol=0050&instrument_type=etf&market=TWSE'
curl 'http://localhost:8000/api/v1/instruments?instrument_type=warrant&market=TPEX&page=1&page_size=20'
```

## 最近十年歷史回補

這是下載指定歷史日期的新命令；原有 `backfill` 仍只重播 archive。沿用官方來源，不包含法人、財報、持股、即時行情或還原股價。

```bash
docker compose build backend history-worker
docker compose run --rm backend alembic upgrade head

# 建立工作，不會在 plan 時下載。以下為本次要求的十年日期範圍。
docker compose run --rm backend stockapp history plan --from 2016-11-05 --to 2026-11-05 --market all
# 省略 --from/--to 時，使用執行主機的台北當日與往前十年。

# 把 plan 回傳的 id 填入；背景執行，關閉終端不會停止容器。
HISTORY_JOB_ID=<JOB_UUID> docker compose --profile history up -d history-worker
docker compose --profile history logs --tail=20 history-worker
docker compose run --rm backend stockapp history status --job <JOB_UUID>

# 可改為前景分批處理；不要與同一個背景工作同時執行。
docker compose run --rm backend stockapp history run --job <JOB_UUID> --max-days 20
# 只在需要時重試 failed 日期；partial/no_data 不會被無限重抓。
docker compose run --rm backend stockapp history run --job <JOB_UUID> --retry-failed

# 離線重播已保存的歷史行情，保留原取得時間。
docker compose run --rm backend stockapp history replay --market twse --date 2016-11-07
```

工作從最近日期往前跑，每個市場／日期提交後保存 checkpoint；重啟時接續未完成日期。`--max-days` 是市場／日期工作數，`--max-seconds` 在日交易之間檢查。預設以建立工作時的 DB 大小為基準，新增達 100 GiB 時停止在日界線；可在 plan 時以 `--max-growth-gb` 設定 1–1000 GiB。這是共享資料庫增量防護，不是精確的單一工作用量或磁碟可用空間保證。十年兩市場為 7,306 個市場／日曆日，需長時間執行，完成時間以實際進度為準。

**歷史權證代碼可重複使用**。目前只將已核對當期官方名錄、幣別、單位及掛牌期間的證券匯入 canonical；已到期／下市、代碼重用或無法確認身分的列完整保存 raw 並標 `unresolved`，不接到今天同代碼的股票／權證。基本資料為工作建立時的官方名錄，尚非完整歷史證券身分資料庫。因此 job 的 `completed_with_gaps` 表示日期已處理，不等於十年所有證券的 K 線完整。空回應記 `no_data`，不自動宣稱休市。

獨立背景 worker 只由 `history` Profile 明確啟動；不建立自動排程，不公開 port。傳輸／權限等永久錯誤會停止，預設不自動重啟。完整來源契約、限制及驗證見[歷史回補規格](docs/specs/taiwan-history-backfill.md)。

`completed_with_gaps` 代表日期都已嘗試，不代表沒有下載失敗；請另外檢查 `counts.failed`。來源恢復後，可先在沒有其他同 job runner 時執行 `history run --job <JOB_UUID> --retry-failed --max-days 2` 驗證少量失敗日期，再以原 UUID 啟動背景 worker 接續剩餘 pending。不要重新 plan 同一範圍。HTTP200 的 TWSE 維護HTML會記 `provider_maintenance`，不當成空資料；同一市場連續三次失敗即停止，另一市場成功不會清零它的失敗計數。301／302／303／307／308等redirect不跟隨、停止本次工作。

### 延伸到目前已確認的最早日期

目前Adapter核實起點為TWSE 2004-02-11、TPEx 2007-04-23，2026-11-07另外少量實測兩日皆回HTTP200、正確日期及可解析行情（980／711列）。不代表所有日期／歷史證券身分皆已完整。

單個job最多3,660日曆日，因此拆成不重疊的兩段：`2007-04-23～2016-11-04 --market all`（6,968個市場／日期）與`2004-02-11～2007-04-22 --market twse`（1,167個）。先查已有工作，**不要重複plan**。本機已建立以下順序清單：

```bash
# 更新本機手動worker，無需重建網站；勿在另一個同job runner執行時啟動。
docker compose build history-worker
HISTORY_JOB_IDS=efa06b94-86ec-41cf-a81a-cbf746b3ab35,0bd32fb5-b135-478f-9bc8-80e191289a61,ba1d630c-7139-4d87-96e3-361ffb58bd6f docker compose --profile history up -d history-worker
docker compose --profile history logs --tail=10 -f history-worker
docker compose run --rm history-worker stockapp history status
```

第一段為原十年工作的失敗日期回補，再自動接續已明確指定的早期兩段。這是一次手動批次清單，沒有排程／自動建立新工作；進度仍由每個job的PostgreSQL checkpoint保存。某段還有未完成／下載failed或遇停止條件時，queue停止，**不跳過失敗段**；partial／unresolved的coverage限制保留。Queue不隱式重試failed，必要時先明確執行原job的`--retry-failed`。`HISTORY_JOB_IDS`優先於舊的單一`HISTORY_JOB_ID`。

## 股票搜尋與 K 線（Phase 1D）

1. 啟動 Compose，並先完成 Migration。若資料庫仍空，依下方 Phase 1B 指令手動匯入官方資料；開啟頁面不會自動同步。
2. 在 Windows Chrome／Edge 開啟 <http://localhost:3000>，輸入 `2330` 或公司名稱，可選上市／上櫃與搜尋結果分頁。
3. 選擇股票後進入 `/stocks/<instrument UUID>`，切換日 K／週 K／月 K。圖表可拖曳、滾輪／雙指縮放，十字線顯示 OHLC、股數及期間完整度。

個股頁可選「最近 1 年／5 年／10 年」或輸入開始／結束日期並按「套用日期」，預設最近一年。預設區間依最新已收錄日期回推曆年，起點不早於已收錄最早日期；自訂日期可查詢較早區間，無資料時明示空狀態，不產生示範行情。長區間自動使用週 K，可改月 K；日 K 每次最多 1,000 根，長區間需縮短日期。週／月起點向前對齊週一／月初，以便圖表與指標使用同一完整起始期間；對齊後超過 3,660 日則要求縮短範圍，不靜默截斷。

歷史 worker 與網站使用同一個 PostgreSQL，已提交的新資料可按「重新讀取資料」或重新整理個股頁讀取，不需複製或還原資料庫。重新讀取會保留已套用的自訂日期；預設年限則更新到最新已收錄日期。回補仍在進行時，起訖日期之間可能有缺口，畫面不宣稱完整。股數保持整數，卡片／tooltip 保留 API Decimal 精度，僅圖表繪製邊界轉換 number。

「最早／最新已收錄日期」只描述兩個端點，不代表中間每個交易日皆完整。若相鄰行情的實際觀察日期相隔超過 90 個日曆日，圖表上方會醒目列出日期間隔，並提供「只看最近已收錄區段」。圖表按已收錄 K 棒排列，沒有行情的期間會壓縮顯示；日期間隔也不能用來判定休市／停牌或推算缺少幾個交易日。此按鈕只改查詢，保留真正的較早資料，不補造 OHLCV，且不會重設後端指標暖機。回補未完成時，指標輸入也可能不完整。

若只更新圖表前端，可執行下列指令，歷史 worker 會保持執行：

```bash
docker compose build frontend
docker compose up -d --no-deps frontend
```

價格是未還原日行情。漲跌／漲跌幅若為 null，顯示 `—`，不以任意舊收盤價推算。較早資料以市場本地日期的日曆日差提示，不能把週末視為遺漏交易日；週／月完整度依後端欄位呈現。

瀏覽器驗收（與 Vitest 分開；需要已啟動的前後端，以及 Phase 1B 已保存的真實 2330 行情）：

```bash
npm ci
# 第一次準備 Chromium；Linux 缺少函式庫時可改用 install --with-deps chromium
npm exec --workspace @stockapp/web -- playwright install chromium
npm run test:browser --workspace @stockapp/web
# 如使用其他本機 port
BROWSER_BASE_URL=http://localhost:3001 npm run test:browser --workspace @stockapp/web
```

測試會保存桌機／手機截圖於 `apps/web/test-results/`（已忽略，不提交）。API 失敗／空資料使用測試攔截回應；不寫入資料庫、不向交易所取資料。CI 的 Vitest 不會執行需要已匯入行情的瀏覽器測試。

圖表採 [TradingView Lightweight Charts™](https://www.tradingview.com/)（Apache-2.0），頁面保留 creator notice、連結與內建 attribution logo。

## 技術指標（Phase 1E）

在個股圖表勾選 SMA、EMA、RSI、MACD 或成交量均線，展開「指標參數」調整後按「套用參數」。期數以目前週期的 K 棒計，例如週 K 的 SMA20 是20根週 K。切換日／週／月會保留開關與參數。

目前僅有少量歷史行情時，預設指標會顯示暖機中，這是正常狀態。SMA／EMA／成交量均線至少需要 N 根；RSI(N)需要 N+1 根；MACD 的 signal／histogram 需 slow+signal−1 根。資料缺值不補0，已知缺少開市 session 時重置暖機。全部結果基於未還原價格；adjusted 查詢回422，不以 raw 冒充。

API：`GET /api/v1/stocks/<UUID>/indicators`。例如從搜尋結果取得 instrument UUID 後：

```bash
curl -G 'http://localhost:8000/api/v1/stocks/<UUID>/indicators' \
  --data-urlencode 'timeframe=1d' --data-urlencode 'indicator=ema' \
  --data-urlencode 'parameters={"period":20}' --data-urlencode 'price_basis=raw'
curl -G 'http://localhost:8000/api/v1/stocks/<UUID>/indicators' \
  --data-urlencode 'timeframe=1w' --data-urlencode 'indicator=macd' \
  --data-urlencode 'parameters={"fast_period":12,"slow_period":26,"signal_period":9}'
# 可另加 --data-urlencode 'from=YYYY-MM-DD' --data-urlencode 'to=YYYY-MM-DD'
```

`indicator`：`sma / ema / rsi / macd / volume_sma`。`parameters` 是嚴格 JSON 整數物件，期數1–250，MACD快線必須小於慢線。回應含算法版本、完整參數、raw價格基準、Decimal字串或null、每點狀態、前置歷史與精度策略。指標先從全部**已保存**歷史初始化，再篩選輸出範圍；`from` 不會重設 EMA。未核實全部歷史，不等同完整回測資料。

新增指標測試可單獨執行：

```bash
docker compose run --rm backend python -m pytest tests/test_technical_engine.py tests/test_technical_api.py -q
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest tests/test_technical_integration.py -q
npm test --workspace @stockapp/web
npm run test:browser --workspace @stockapp/web
```

## 股票查詢 REST API（Phase 1C）

查詢只讀 PostgreSQL，不會觸發官方 API 抓取；更新行情仍使用上面的匯入 CLI。

```bash
# 代碼／名稱搜尋、篩選與分頁
curl 'http://localhost:8000/api/v1/instruments?q=2330&market=TWSE&page=1&page_size=20'
curl -G 'http://localhost:8000/api/v1/instruments' --data-urlencode 'name=台積電'
```

從搜尋結果 `items[].id` 取得 UUID，以下 `<UUID>` 替換為實際值：

```bash
curl 'http://localhost:8000/api/v1/instruments/<UUID>'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1d&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1w&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/bars?timeframe=1M&from=2026-11-01&to=2026-11-04'
curl 'http://localhost:8000/api/v1/stocks/<UUID>/latest'
curl 'http://localhost:8000/api/v1/data/status'
```

- 價格／金額以 Decimal JSON 字串回傳，成交量單位為股。
- 週 K 依市場本地星期一至星期日分組，月 K 依當地月份。分開回傳期間是否結束與資料完整度，未知日曆／部分歷史不宣稱完整。
- 最新行情是已保存的日行情 snapshot，`is_realtime=false`。age_calendar_days 是日曆日差，不代表缺少交易日數。
- 未有可核實的官方參考價時，`reference_price`／`change_percent` 為 null；previous_observed 只是最近保存的觀察，不保證是前一交易日。
- 搜尋 page_size 上限100；行情 inclusive range 上限3660日、聚合後上限1000筆，超過回422而不截斷。未知股票回404；已存在但無行情回200空資料。
- `/data/status` 分開顯示最新任意作業與最新外部 sync，保留 partial／failed 狀態，不讓回補掩蓋失敗。

互動文件：<http://localhost:8000/docs>；runtime contract：<http://localhost:8000/openapi.json>。Snapshot 為 [apps/api/openapi.json](apps/api/openapi.json)，變更後產生與檢查：

```bash
.venv/bin/python apps/api/scripts/export_openapi.py
.venv/bin/python apps/api/scripts/export_openapi.py --check
```

# Phase 2 — Shared Engineering Rules (Revised)

Repository: AlHIO/stock-analysis-platform

本專案使用 Spec-Driven Development，所有開發必須遵循既有 AGENTS.md、README.md、Architecture 與 Phase 2 Specifications。

## Development Policy

1. 開始前檢查目前 Repository 的實際狀態、已完成模組與測試。
2. 優先沿用現有 Domain、Application、Infrastructure、API 分層。
3. 不重新實作已完成的功能或計算公式。
4. 可以修改 PostgreSQL Schema、建立 Alembic Migration、更新 Repository 與執行整合測試。
5. 可以執行 Docker Compose、重新建置相關 Container、啟動必要服務。
6. 可以使用已核實且獲准使用的真實官方金融資料進行資料匯入驗證。
7. 可以執行 PostgreSQL、Redis、Backend、Frontend 的完整整合測試。
8. 如果發現既有 Schema 或 API 有設計缺陷，允許提出並實作必要的向後相容修正。

## Data Safety

- 保留 Phase 1 已匯入的真實歷史行情。
- 不得執行 docker compose down -v。
- 不得清空 PostgreSQL Volume。
- 不得使用 DROP TABLE / TRUNCATE 清除既有正式資料。
- 執行 Migration 前先檢查 Schema、Migration History、資料量及可能的資料損失。
- 若 Migration 可能造成資料損失，先停止並提出 Migration Plan。
- 不得使用 synthetic fixtures 冒充真實行情。
- 外部來源授權未確認時，保持 disabled，不得繞過限制。

## Testing

每個階段依實際變更執行：

- Pytest Unit Tests
- PostgreSQL Integration Tests
- Alembic Migration Tests
- Vitest
- TypeScript Typecheck
- Next.js Production Build
- OpenAPI Contract Validation
- Playwright E2E（涉及前端時）
- Docker Compose Health Checks

優先使用現有隔離測試 Schema，避免測試污染已匯入的真實資料。

## Completion

每個階段完成後必須提供：

1. 修改或新增的檔案。
2. Database Migration 與 Schema 變更。
3. 實際執行的測試及結果。
4. 真實資料匯入與 API 驗證結果（如適用）。
5. 發現並修正的問題。
6. 尚未完成的功能或資料來源限制。
7. 下一階段所需的前置條件。

可以執行必要的 Migration、Docker 啟動與測試，但不得自行 commit、push、公開部署或進入下一個 Phase。

只有本次指定的子階段完成後才停止。


## 本機開發

需求：Node.js 22 LTS、npm、Python 3.11+（建議 3.12）、venv/pip、Docker。

```bash
test -f .env || cp .env.example .env
npm ci
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r apps/api/requirements.lock
.venv/bin/python -m pip install --no-deps -e './apps/api[test]'
docker compose up -d --wait postgres redis
```

請先確認 `python3.12 --version` 可用；若使用其他受支援版本，改用對應的 Python 3.11+ 執行檔建立 venv。Ubuntu 預設的 `python3` 可能仍是 3.10，既有 venv 也不會自動升級。

終端一（backend；由 `apps/api` 載入根目錄 `.env`）：

```bash
cd apps/api
../../.venv/bin/uvicorn stockapp.main:app --reload --host 0.0.0.0 --port 8000
```

終端二（frontend）：

```bash
npm run dev
```

前端預設連到 `http://localhost:8000`。如需變更，在啟動時設定 `BACKEND_URL=http://localhost:8001 npm run dev`；Next.js 不自動讀取 monorepo 根目錄 `.env`。`BACKEND_URL` 只供伺服器使用，不能包含用戶端金鑰。

`.env.example` 中的 host URL 用於本機開發；Compose 會覆寫為 `postgres`、`redis`、`backend` 服務名稱。更改服務 port 時，本機 URL 也須同步調整。資料庫密碼若含 URL 特殊字元，須對 DATABASE_URL 的密碼部分 URL encode；Compose 的本機範本使用簡單開發密碼。

## 測試與建置

不依賴外部服務的測試：

```bash
.venv/bin/python -m pytest apps/api/tests -m 'not integration and not network'
npm test
npm run typecheck
npm run build
```

有 PostgreSQL 與 Redis 的整合測試：

```bash
docker compose up -d --wait postgres redis
cd apps/api
RUN_INTEGRATION=1 ../../.venv/bin/python -m pytest -m integration
```

也可以完全在容器內測試（backend 容器會繼承 Compose 資料庫 URL）：

```bash
docker compose run --rm backend python -m pytest -m 'not integration and not network'
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m integration
docker build --target build -f apps/web/Dockerfile -t stockapp-web-check .
docker run --rm stockapp-web-check npm test
docker run --rm stockapp-web-check npm run typecheck
```

外部官方網路測試單獨啟用，預設不在 CI 執行，也不寫入資料庫：

```bash
docker compose run --rm -e RUN_NETWORK=1 backend python -m pytest -m network
```

Pytest 涵蓋健康檢查、Domain、PostgreSQL 約束、Repository、Migration 升降版與並發重送；DB 測試使用獨立隨機 schema，不修改既有 public 資料。Vitest 涵蓋 API 讀取及畫面狀態。整合測試預設略過，明確設定 `RUN_INTEGRATION=1` 才會真正連線。npm 使用 package-lock.json；Python 使用 requirements.lock，更新方式見 Phase 1A 規格。

CI 執行上述測試、Next.js production build，並在 Docker Compose 內啟動四項服務、執行 `alembic upgrade head`／`alembic check` 與整合測試。實際驗證結果見 [Phase 0 驗證紀錄](docs/specs/phase-0-validation.md) 及 [Phase 1A 規格與驗收](docs/specs/phase-1a-database.md)。

## 後續開發

遵循 [AGENTS.md](AGENTS.md) 與 [架構決策](docs/architecture/foundation.md)。本次止於 Phase 2G-2系統驗收；回測、選股器、警示、AI 分析、即時行情與真實下單不在本次範圍，不自行進入Phase 3。
