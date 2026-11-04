# Stock Analysis Platform · Phase 0

台灣上市／上櫃股票分析平台基礎建設。採 Monorepo、Modular Monolith + Data Workers；本階段只有服務狀態頁與健康檢查，**沒有行情、指標或假股票資料**。

## 目錄與邊界

```text
apps/
  web/                    Next.js App Router、TypeScript、Tailwind CSS、Vitest
  api/
    src/stockapp/
      modules/health/     api → application → domain；infrastructure 實作 domain ports
      shared/domain/     資料來源 DataSource Protocol
      config.py          環境設定
      main.py            FastAPI composition root
    tests/               Pytest 單元與真實服務整合測試
workers/                 Data Workers 預留邊界，目前不啟動 worker
packages/contracts/      API 合約／未來產生的 client
docs/architecture/      架構決策
docs/specs/             範圍與驗收
compose.yaml             PostgreSQL、Redis、API、Web
```

資料供應商須透過 Domain 定義的 Adapter 介面接入；API 不直接呼叫外部行情來源。API 與 worker 未來共用 Python domain/application，無需先拆成微服務。尚未建立股票資料表，因此本階段沒有 migration。

## Docker 啟動（建議）

需求：Docker Engine／Docker Desktop 正常啟動、Docker Compose v2（支援 `up --wait`）。WSL 必須在 Docker Desktop 開啟此 distro 的 WSL integration。

以下指令皆從本 README 所在的 repository 根目錄執行：

```bash
cp .env.example .env
# .env 使用本機開發佔位密碼；請勿用於公開部署，也不要提交 .env。
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose ps
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
```

開啟 <http://localhost:3000>。API 文件：<http://localhost:8000/docs>。

- `/health`：API 存活時回傳 HTTP 200、`{"status":"ok","service":"stockapp-api"}`，不要求資料庫在線。
- `/health/ready`：實際執行 PostgreSQL `SELECT 1` 與 Redis `PING`，全部成功回傳 HTTP 200；失敗回傳 HTTP 503，`status=degraded` 及各服務狀態，不揭露連線字串。
- 首頁由 Next.js server 向後端讀取 readiness，重新整理更新狀態；瀏覽器不需要存取 Docker 內部 hostname，也不需要 CORS。
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

## 本機開發

需求：Node.js 22 LTS、npm、Python 3.11+（建議 3.12）、venv/pip、Docker。

```bash
cp .env.example .env
npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -e './apps/api[test]'
docker compose up -d --wait postgres redis
```

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
.venv/bin/python -m pytest apps/api/tests -m 'not integration'
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
docker compose run --rm backend python -m pytest -m 'not integration'
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m integration
docker build --target build -f apps/web/Dockerfile -t stockapp-web-check .
docker run --rm stockapp-web-check npm test
docker run --rm stockapp-web-check npm run typecheck
```

Pytest 涵蓋存活、依賴失敗及例外處理；Vitest 涵蓋 API 讀取、503／離線／逾時／格式異常及畫面狀態。整合測試預設略過，明確設定 `RUN_INTEGRATION=1` 才會真正連線。npm 使用提交的 lockfile；Python 目前透過 pyproject 的版本範圍安裝，尚未建立完整依賴鎖檔。

CI 執行上述測試、Next.js production build，並在 Docker Compose 內啟動四項服務與驗證連線。執行環境的實際驗證結果見 [Phase 0 驗證紀錄](docs/specs/phase-0-validation.md)。

## 後續開發

遵循 [AGENTS.md](AGENTS.md) 與 [架構決策](docs/architecture/foundation.md)。即時行情、K 線、技術指標、法人、大戶、基本面與篩選都在後續 Phase；本次不實作。
