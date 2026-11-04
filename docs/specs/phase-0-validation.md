# Phase 0 驗證紀錄

日期：2026-11-04（Asia/Taipei）。只涵蓋 Foundation，不實作任何股票功能。

## 已執行

| 驗證 | 結果 |
| --- | --- |
| Pytest：`python -m pytest apps/api/tests -vv` | 6 passed、1 skipped；使用隔離的 Python 3.12 環境 |
| Vitest：`npm test` | 2 個測試檔、7 項測試通過 |
| TypeScript：`npm run typecheck` | 通過 |
| Next.js production：`npm run build` | 通過，使用 Webpack |
| npm 安裝安全稽核 | 更新 Vitest 後回報 0 vulnerabilities |
| 實際 HTTP `/health` | HTTP 200，`status=ok` |
| 實際 HTTP `/health/ready`（無 PostgreSQL／Redis） | HTTP 503，`status=degraded`，符合預期 |
| Production standalone 首頁 | HTTP 200，顯示平台標題、FastAPI 已連線及相依服務尚未就緒 |
| Production CSS | HTTP 200 |
| Compose YAML | 成功解析並確認四個 service；不代表 Compose runtime 驗證 |
| `git diff --check`、`git diff --cached --check` | 通過 |

HTTP 驗證以本機短暫啟動 FastAPI 與 Next.js standalone server 執行，結束後已停止。沒有使用假行情或假相依服務冒充實際連線；首頁尚未執行瀏覽器視覺檢查。

## 環境限制與待驗收項目

- 此 WSL distro 的 Docker Desktop integration 未啟用，`docker version`／`docker compose version` 均提示 Docker 不可用。因此未執行 Docker image build、`docker compose config`、Compose 四服務啟動及真正的 PostgreSQL／Redis 連線驗證。
- 真實服務整合測試 `test_real_dependencies` 已建立，但本次略過。必須依 README 啟動服務並執行 `RUN_INTEGRATION=1` 後才能確認。
- 初次 Turbopack build 因子程序的本機 socket 權限而失敗；production build 改用 Next.js 支援的 Webpack 模式後通過。Pytest TestClient 在沙箱內受限，沙箱外測試通過。
- 安裝時發現 Vitest 3 的依賴漏洞，已更新為 Vitest 5 並重建 package-lock.json、重跑測試與型別檢查。
- 目前 Python 相依使用 pyproject 版本範圍，尚無完整鎖檔。最新 Starlette 對目前 httpx TestClient 產生一項 deprecation warning，測試仍通過。
- GitHub Actions workflow 已建立，尚未在遠端執行；包含四服務啟動與真實依賴整合測試。
- 使用者提供的 ChatGPT 對話連結無法讀取，需求以本次訊息為準。

## 環境恢復後的驗收

```bash
cp .env.example .env  # 只在尚未設定 .env 時執行，避免覆蓋自己的設定
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose ps
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:3000
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m integration
docker compose down
```
