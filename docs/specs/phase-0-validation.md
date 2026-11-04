# Phase 0 驗證紀錄

日期：2026-11-04（Asia/Taipei）。Foundation 必要驗收已通過；只涵蓋服務基礎建設，不實作股票功能。

## 驗證環境

- WSL Ubuntu，Docker Desktop WSL integration 已啟用。
- Docker Engine 29.9.2、Docker Compose v5.6.1。
- API image：Python 3.12.16；前端 image：Node.js 22、Next.js 16.4.5。
- `.env` 由開發範本建立，已確認被 `.gitignore` 排除且未受 Git 追蹤。

## 實際結果

| 驗證 | 結果 |
| --- | --- |
| `docker compose config --quiet` | 通過 |
| `docker compose up --build --wait --wait-timeout 300` | 成功建置映像並啟動四項服務 |
| `docker compose ps` | postgres、redis、backend、frontend 全部 healthy |
| PostgreSQL `SELECT 1` | 成功回傳 1 |
| Redis `PING` | 成功回傳 PONG |
| 容器內 Pytest（含真實服務整合測試） | 7 passed，沒有略過測試 |
| 容器內 Vitest | 2 個測試檔、7 項測試通過 |
| 容器內 TypeScript | 通過 |
| 本機 Vitest／TypeScript | 7 項測試及型別檢查再次通過 |
| Next.js production build | Docker build 內成功，使用 Webpack |
| npm ci 安全稽核 | Docker build 回報 0 vulnerabilities |
| `/health` | HTTP 200，`status=ok` |
| `/health/ready` | HTTP 200，postgres 與 redis 都為 ok |
| Production 首頁與 CSS | HTTP 200；頁面顯示 FastAPI、PostgreSQL、Redis 已連線 |
| Windows → WSL | Windows PowerShell 請求 localhost:3000 回傳 200；localhost:8000/health/ready 回傳全部 ok |
| `git diff --check`、`git diff --cached --check` | 通過 |

HTTP 驗證連線到實際 Compose 服務；資料庫連線另以 Pytest 整合測試及 psql 驗證。沒有使用假行情或假服務冒充連線。前次缺少 Docker 的限制已解除，先前手動啟動的 Next.js／Uvicorn 已停止，目前由 Compose 提供服務。

## 重現指令

從 repository 根目錄執行：

```bash
test -f .env || cp .env.example .env
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose ps
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec -T redis redis-cli ping
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -vv -p no:cacheprovider
docker build --target build -f apps/web/Dockerfile -t stockapp-web-check .
docker run --rm stockapp-web-check npm test
docker run --rm stockapp-web-check npm run typecheck
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:3000
```

Windows 瀏覽器開啟 <http://localhost:3000>；API 文件在 <http://localhost:8000/docs>。服務已保留運行。完成查看後，可執行 `docker compose down` 停止服務並保留資料。

## 非阻擋項目與驗證範圍

- 目前 Starlette 對 httpx TestClient 產生 1 項 deprecation warning；7 項後端測試皆通過。
- Phase 0 當時 Python 相依尚無完整鎖檔；Phase 1A 已新增 requirements.lock，見對應規格。
- GitHub Actions workflow 已建立，其對應建置及測試已在本機／容器執行；尚未在遠端執行 workflow。
- 已驗證 HTTP、HTML 服務狀態與 CSS，尚未執行瀏覽器視覺檢查。
- 初次本機 Turbopack build 受子程序 socket 權限限制；目前採用 Next.js 支援的 Webpack 模式，容器 production build 已通過。
- 使用者提供的 ChatGPT 對話連結無法讀取，需求以本次訊息為準。

以上不影響 Phase 0 必要驗收。即時行情、K 線、指標及其他股票功能仍在後續 Phase，本次停止於 Foundation。
