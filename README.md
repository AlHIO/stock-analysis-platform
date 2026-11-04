# Stock Analysis Platform · Phase 1D

台灣上市／上櫃股票分析平台。採 Monorepo、Modular Monolith + Data Workers；目前包含股票搜尋、日／週／月 K 線與成交量頁面、多市場資料模型、TWSE／TPEx 官方日行情匯入 CLI，以及股票查詢 REST API。行情僅讀 PostgreSQL 已保存資料，非即時報價；沒有技術指標或即時交易功能。

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
      shared/domain/     資料來源 DataSource Protocol
      shared/infrastructure/ SQLAlchemy Base 與 engine factory
      config.py          環境設定
      main.py            FastAPI composition root
      cli.py             官方同步、archive 回補與匯入狀態查詢
    tests/               Pytest 單元與真實服務整合測試
    migrations/          Alembic schema revisions（啟動時不自動執行）
    requirements.lock    含雜湊的 runtime／test 依賴鎖檔
    openapi.json         與 runtime 比對的 API contract snapshot
workers/                 Data Workers 預留邊界，目前不啟動 worker
packages/contracts/      API 合約／未來產生的 client
docs/architecture/       架構決策
docs/specs/              範圍與驗收
compose.yaml             PostgreSQL、Redis、API、Web
```

資料供應商須透過 Domain 定義的 Adapter 介面接入；API 不直接呼叫外部行情來源。API 與 worker 未來共用 Python domain/application，無需先拆成微服務。Phase 1A 建立六個核心模型與獨立標準化 revision 表，未新增股票 API 或 UI；完整 schema、去重規則與安全回退方式見 [Phase 1A 規格](docs/specs/phase-1a-database.md)。

Phase 1B 的管線、逐筆異常與重試規則見 [匯入規格](docs/specs/phase-1b-ingestion.md)，實際端點、欄位、授權與更新頻率見 [官方來源查證](docs/specs/phase-1b-sources.md)。

Phase 1C 的 API、聚合／完整度、查詢上限與錯誤碼見 [股票查詢 API 規格](docs/specs/phase-1c-market-api.md)。此階段未修改核心資料模型，也沒有新 Migration。

Phase 1D 的前端架構、圖表語意及驗收見 [股票圖表前端規格](docs/specs/phase-1d-stock-chart.md)。沿用 Next.js App Router，使用固定版本 Lightweight Charts 5.2.1；沒有改動後端核心模型或抓取新行情。

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

# 檢查匯入數量、錯誤、來源端點、原始回應時間與 SHA-256
docker compose run --rm backend stockapp runs --limit 5
docker compose run --rm backend stockapp runs --id <RUN_UUID>

# 重新處理已保存的官方 raw；日期必須有對應 archive
docker compose run --rm backend stockapp backfill --market twse --date 2026-11-03
docker compose run --rm backend stockapp backfill --market tpex --date 2026-11-04
```

`latest` 是來源提供的資料日期，不保證是執行當天。**backfill 僅支援資料庫已保存的官方原始回應，不提供未確認的遠端歷史下載**；可重複 `--date` 指定多天，無 archive 時明確失敗，絕不替換最新行情的日期。

退出碼 0 代表成功；2 代表 partial／資料失敗。遇到個股缺價或名錄有股票但行情缺列時，run 會標 failed + Partial coverage，但有效股票仍會保留；請讀 accepted／rejected／missing 與逐筆原因。重送不產生重複 canonical bar。1 代表設定／DB 等操作錯誤。

原始資料提供：臺灣證券交易所（上市個股日成交資訊／上市公司基本資料）、證券櫃檯買賣中心（上櫃股票行情／上櫃公司基本資料）。依[政府資料開放授權條款第1版](https://data.gov.tw/license)利用；本平台另做股票範圍篩選、標準化與驗證，不代表交易所背書。單位與限流查證見來源規格，預設5秒間距是專案的保守設定。

## 股票搜尋與 K 線（Phase 1D）

1. 啟動 Compose，並先完成 Migration。若資料庫仍空，依下方 Phase 1B 指令手動匯入官方資料；開啟頁面不會自動同步。
2. 在 Windows Chrome／Edge 開啟 <http://localhost:3000>，輸入 `2330` 或公司名稱，可選上市／上櫃與搜尋結果分頁。
3. 選擇股票後進入 `/stocks/<instrument UUID>`，切換日 K／週 K／月 K。圖表可拖曳、滾輪／雙指縮放，十字線顯示 OHLC、股數及期間完整度。

每次圖表查詢以最新已收錄日期往前最多365天，並受已保存起日限制；不是全部歷史。無資料時明示空狀態，不產生示範行情。股數保持整數，卡片／tooltip 保留 API Decimal 精度，僅圖表繪製邊界轉換 number。

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

遵循 [AGENTS.md](AGENTS.md) 與 [架構決策](docs/architecture/foundation.md)。本次止於 Phase 1D；即時行情、技術指標、法人、大戶、基本面與篩選仍在後續 Phase。
