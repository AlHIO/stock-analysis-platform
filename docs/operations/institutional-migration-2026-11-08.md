# Institutional Migration — 2026-11-08

依使用者明確要求，完成本機 `stockapp` database／`public` schema 升級，並驗證現有網站。沒有開發法人 API／UI、匯入法人資料、重建部署或啟動回補 Worker。

## 升級結果

| 項目        | 結果                                                              |
| ----------- | ----------------------------------------------------------------- |
| PostgreSQL  | 17.11                                                             |
| 原版本      | `0004_history_backfill`                                           |
| 新版本      | `0006_institutional_jobs`（包含0005、0006）                       |
| 執行方式    | 專案 venv 的 Alembic，使用同一筆 PostgreSQL transaction           |
| 鎖與逾時    | transaction advisory lock；lock_timeout=2s、statement_timeout=30s |
| Schema 比對 | `alembic check`：No new upgrade operations detected               |

0005新增法人 source／raw／item audit、revision／canonical，共五表與必要複合參照約束；0006新增 ingestion job checkpoint 表。六張新表在升級後皆為0列。既有 DailyBar 與行情 revision 沒有 DML 變更，沒有加入測試資料。

升級前檢查：history-worker容器已為 `Exited (2)`，最新回補工作為 interrupted，另有 planned 工作；PostgreSQL沒有活躍的回補連線。這不代表歷史回補已完成。本次沒有停止、重啟、重建任何容器，也沒有恢復回補工作。

## 網站驗證

- `/health`、`/health/ready`、前端首頁皆HTTP200；PostgreSQL／Redis readiness正常。
- 前端同源proxy可搜尋真實已保存2330；升級前後最新bar完全一致，交易日為2026-11-05。
- 2026-10-01～2026-11-05查詢：日K23根、週K6根、月K2根，皆HTTP200且非空。不以此宣稱全部歷史完整。
- Playwright桌機／手機驗收：**2 passed**，包含搜尋、日週月切換、十字線、zoom／pan與responsive resize。
- 基本回歸：後端離線 **1,191 passed、299 deselected**；前端 Vitest **244 passed**；Typecheck／Production Build通過。本機build沒有部署到容器，產生的next-env.d.ts變更已還原。

瀏覽器指令（使用專案既有的隔離Chromium）：

```bash
PATH="$PWD/.tools/node/bin:$PATH" \
PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers" \
npm run test:browser --workspace @stockapp/web -- e2e/stock-chart.spec.ts \
  --grep "search 2330 and inspect real|mobile name search"
```

第一次未指定專案瀏覽器路徑時，Playwright找不到Chromium；改用既有`.tools/playwright-browsers`，沒有下載或安裝套件。桌機驗收最初亦因圖表位於viewport下方、測試直接移動滑鼠而未觸發十字線；已在取得座標前加入`scrollIntoViewIfNeeded()`，重跑兩項通過。此為測試定位修正，產品前端程式未修改。

截圖位於被git忽略的`apps/web/test-results/`，沒有commit。前端確認網址：<http://localhost:3000>。WSL使用者可在Windows瀏覽器開啟。

本次Migration只準備資料層，現有網站畫面維持既有股票搜尋／K線功能，沒有新增法人頁面。原Phase2B-4規格中的「未執行正式Migration」是該次實作時的紀錄；本文件記錄後續明確授權的本機升級。
