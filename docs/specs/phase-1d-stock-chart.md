# Phase 1D — Stock Chart Frontend

## 範圍

建立可搜尋、閱讀已保存行情的前端：代號／名稱搜尋、上市／上櫃篩選、個股資訊、日／週／月 K 線、成交量及資料來源。沒有新增資料抓取、排程、WebSocket、技術指標、核心資料模型或 Migration。停止於 Phase 1D。

依據 [Phase 1C contract](../../apps/api/openapi.json) 與 [聚合／freshness 規格](phase-1c-market-api.md)。不將測試 fixture、固定價格或固定日期帶入正式頁面。

## 架構與檔案

| 邊界 | 實作 |
| --- | --- |
| App Router | `apps/web/src/app/page.tsx`、`stocks/[id]/page.tsx`；UUID 識別股票，切換 instrument 時重建 detail state |
| 同源 GET proxy | `app/api/market/[...path]/route.ts`，固定 FastAPI `/api/v1` upstream、路由與 query allowlist |
| 型別／client | `lib/market-types.ts` 對照 OpenAPI 並做 runtime guards；`lib/market-api.ts` 處理 HTTP／timeout／取消 |
| 搜尋 | `components/stock-search.tsx`，250ms debounce、IME、取消過期請求、鍵盤 combobox、證券結果分頁 |
| 個股頁 | `components/stock-detail.tsx`，latest 與圖表載入狀態分離，週期切換取消舊請求 |
| 圖表 | `components/stock-chart.tsx`、CSS module；`lib/chart-data.ts` 負責驗證及繪圖資料轉換 |
| 精度／顯示 | `lib/decimal.ts`、`lib/format.ts`，字串處理一般／指數 Decimal，不做浮點金融計算 |
| 測試 | `tests/*.test.ts(x)` 為離線 Vitest；`e2e/stock-chart.spec.ts` 為獨立 Chromium 驗收 |

瀏覽器 → 同源 `/api/market/*` → Next.js server → `BACKEND_URL` 的 FastAPI → PostgreSQL。瀏覽器無需 Docker hostname 或 CORS；`BACKEND_URL` 沒有公開成 `NEXT_PUBLIC_*`，沿用現有環境設定。

Proxy 只接受已知 GET routes／參數，拒絕任意 URL、重複 query、未知參數與 upstream redirect。Upstream timeout 12秒，client timeout 15秒；fetch／response 皆 `no-store`。連線錯誤為503；保留安全的404／422／503，不轉送原始內部診斷或認證資訊。後端健康檢查與資料模型不變，首頁仍可展開服務狀態。

## 搜尋與個股內容

- 支援代號或名稱、全部／TWSE上市／TPEX上櫃；搜尋結果只提供 STOCK 的個股連結。
- 後端搜尋包含其他證券類型，因此每頁20筆**證券**中篩選股票，顯示總證券筆數及上一頁／下一頁；本頁無股票不代表其他頁也沒有。最多遵守 API 1000頁上限，沒有假造完整搜尋結果。
- Loading、API unavailable、一般錯誤、404、空搜尋及空行情均有獨立文案／重試；舊請求不覆蓋新條件。
- 最新資料顯示名稱、symbol、市場、currency、日收盤價、成交量（股）、交易日、匯入時間（市場時區）及來源。
- `reference_price/change/change_percent` 為 null 時顯示 `—`。目前 Phase 1C 未提供可核實官方參考價，因此不宣稱已計算正式漲跌幅，不以前一日曆日或任意前筆觀察值替代。

## 圖表語意與生命週期

固定 `lightweight-charts=5.2.1`，依官方 [v5 API](https://tradingview.github.io/lightweight-charts/docs) 使用 `addSeries(CandlestickSeries/HistogramSeries)`；兩個 pane 共用時間軸，提供 crosshair、tooltip、zoom／pan／touch。

價格與成交量直接使用 FastAPI 聚合結果，前端不另行計算週／月 OHLCV。週／月時間軸以 `trade_date`（最後已觀察的交易日）定位，tooltip 列出實際觀察起訖、session 數、period_closed、completeness；不把週日／月底當作有交易的日期。

查詢範圍預設錨定 `history.available_to` 往前一曆年，起點不早於 `available_from`。歷史回補啟用後，新增最近 1／5／10 年及自訂日期控制，讓較早的已保存資料可由網站查詢。預設區間並不代表歷史完整；自訂日期在按下「套用日期」才送出，空值、無效日期、起訖倒置或超過 3,660 日會在前端阻擋。

超過 1,000 個日曆日的區間保守切換週 K（原選月 K 時保留），並停用日 K；使用者可縮短日期查看每日資料。這是在回補完成前後都能遵守單次最多 1,000 根輸出的限制，不能由目前資料稀疏就無界限抓十年日 K。週／月起點向前對齊完整 bucket，圖表與指標使用相同 from/to；若對齊導致超過 3,660 日，顯示縮短範圍提示而不截成 bucket 中途。

「重新讀取資料」重新讀取 FastAPI 已提交資料；保留自訂區間，年限預設則隨 latest history extent 更新。Provider 不會因點擊而重新抓取，背景回補 worker 維持獨立。無資料日期、非交易日不補零、不延續前收、不產生假 K 棒。只有一筆就顯示一筆。`lib/history-range.ts` 封裝純日期計算，沒有加入全域狀態或新依賴。

### 已觀察日期之間的長間隔

Lightweight Charts 的 logical time index 只會排列提供的觀察點；只有 2016 單日加上 2026 近期行情時，兩段會相鄰顯示。這不是日期轉換，也不能把 `history.available_from/to` 解讀為連續覆蓋。

`lib/history-gaps.ts` 以排序後前一 bar 的 `last_trade_date` 與下一 bar 的 `first_trade_date` 偵測超過 90 日曆日的間隔，週／月 K 不以 period_start/end 當觀察交易日。90 日只是保守的注意提示，不是交易日曆或缺漏判定。圖表上方明示日期間隔、壓縮顯示與未確認完整性，並提供最近一個長間隔之後的區段切換。原始 bars 不被填補、刪除或改值；無效、重疊或矛盾的觀察日期會阻擋圖表並提示重新讀取。

最近區段按鈕保留目前週／月週期，仍由既有 API 聚合，並向前對齊完整 bucket 以維持圖表／指標一致。它不刪除 2016 真實資料，也不宣稱較近區段的交易日曆已完整。指標仍以全部已保存的前綴暖機；間隔提示說明缺漏可能影響暖機／數值，不另寫前端公式或假定缺少幾個交易日。

本次實際診斷時，2330 只有 49 筆日行情：先前單日驗證的真實 2016-11-07，加上倒序回補到的 2026-08-25～2026-11-05。當時工作處理 146／7,306 個市場日期，仍持續執行；此數字僅為診斷快照，不代表現時進度。沒有移除該 2016 行情或補造中間 K 棒。

間隔提示補充驗證：`npm test` 244 passed（含間隔 helper 16 項、StockDetail 22 項）；Typecheck 與 Docker production build 通過。後端離線 Pytest 683 passed、208 deselected，本次未重跑完整 DB／外網測試集。Chromium 8 passed，新增真實稀疏歷史區間提示與最近區段操作，且核對切換後最早真實觀察仍保存在 API；原有歷史日期、指標、手機及錯誤／空資料驗收保留。僅更新 frontend，history-worker 容器 ID 前後相同、持續執行。沒有新的後端 schema 或 migration。

Decimal 文字在卡片與 tooltip 保留字串精度，包含 `1E-8`／`0E-8`；指數／長度受界限限制。圖表 library 以 JS number 定位像素，僅在繪製邊界轉換；金融文字與漲跌不經 float 計算。拒絕非有限／不安全價格、轉換後無法區分的 OHLC 差異、非法 OHLC、重複日期與非安全整數股數，避免靜默畫錯。上漲紅／下跌綠是**收盤對開盤**的 K 棒顏色，明示並非官方昨收漲跌。

Chart 只在 Client Component effect 動態載入。取消過期 import、React StrictMode 重複掛載、跨週期重建與初始化中途失敗皆清理 chart。ResizeObserver 依容器寬高 resize；cleanup disconnect、unsubscribe、remove。CSS 提供桌機／手機高度；頁面沒有額外全域 store。

圖表保留 `layout.attributionLogo=true` 及 [TradingView](https://www.tradingview.com/) creator link／[5.2.1 NOTICE](https://github.com/tradingview/lightweight-charts/blob/v5.2.1/NOTICE) 的 attribution。Lightweight Charts 採 Apache-2.0。

## 空資料、完整度與資料較舊

- latest.bar=null 或 bars=[] 時明確顯示無資料，不建立假的圖表。
- latest 成功但圖表失敗時保留最新快照，提供獨立圖表重試。
- age_calendar_days>0 顯示「較早日期的已收錄資料」，列出日期及日曆日差；明示不是遺漏交易日數，不因週末／假日斷言 provider stale。
- future_dated 顯示異常日期提醒，不冒充目前行情。
- 週／月尚在進行或 completeness 不明時保留警示；不自行推斷 expected sessions。`history.completeness=not_verified` 始終清楚揭示。
- 所有價格標示日行情快照／非即時／未還原權息；匯入時間不是即時報價時間。

## 執行與測試

從 repository root：

```bash
npm ci
npm test
npm run typecheck
NEXT_TELEMETRY_DISABLED=1 npm run build
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m 'not network' -q -p no:cacheprovider
```

瀏覽器驗收獨立啟用，需要已啟動前後端、完成 Migration，以及 Phase 1B 先前匯入的**真實 2330**。沒有行情時明確測試失敗，不自動塞 fixture：

```bash
npm exec --workspace @stockapp/web -- playwright install chromium
# Linux 缺少 browser runtime libs 時：playwright install --with-deps chromium
npm run test:browser --workspace @stockapp/web
```

`BROWSER_BASE_URL` 可指定其他本機 URL。WSL 由 Windows Chrome／Edge 開啟 <http://localhost:3000>；不需要在 Ubuntu 打開 GUI 瀏覽器。瀏覽器測試以 Chromium 自動驗證並保存 desktop/mobile screenshot（gitignored `apps/web/test-results/`），API 錯誤／空資料僅使用測試 route 攔截，不修改 DB。

離線測試涵蓋搜尋／市場／分頁／IME／取消亂序、錯誤與空資料、proxy allowlist／timeout／安全錯誤、Decimal 精度／指數、日期間隙／非法 OHLC／重複與危險數值、chart lifecycle、日週月切換。後端另執行 PostgreSQL／Redis 整合回歸。CI smoke 改驗收新的搜尋頁及同源 instruments proxy；不在 CI 自動下載真實交易所行情。

## 歷史日期控制補充驗收

歷史回補啟用後新增的日期控制，已在同一套 Next.js App Router／FastAPI／PostgreSQL 架構下驗證：

| 檢查 | 本次結果 |
| --- | --- |
| `npm test` | 222 passed；含日期範圍純函式 25 項及 StockDetail 16 項 |
| `npm run typecheck` | 通過 |
| `docker compose build frontend` | Next.js production build／TypeScript 通過 |
| Pytest `-m 'not integration and not network'` | 683 passed，208 deselected；本次未重跑完整 PostgreSQL／外網測試集 |
| Chromium／Playwright | 7 passed，含真實已保存 2016-11-07 的 2330 日／週／月及 SMA(1) 日期／數值一致性 |
| Worker 保留 | 僅 `docker compose up -d --no-deps frontend` 更新前端；history-worker 容器 ID 前後相同、持續執行 |

測試查詢的歷史行情來自已保存的 TWSE_HISTORY，不產生假 K 棒，也未因瀏覽器驗收觸發交易所下載。手機尺寸與無資料／API 失敗測試繼續通過。原有基本健康服務正常，沒有新增後端 route、Migration、套件或重啟資料庫。回補尚未完成，畫面中的已收錄起訖日不代表中間每個交易日皆完整。README 已補上網站操作及僅更新前端的指令。

## 原 Phase 1D 實際驗收

2026-11-04（Asia/Taipei）於 WSL Ubuntu + Docker Desktop 執行：

| 驗證 | 實際結果 |
| --- | --- |
| `npm test` | **128 passed**，10個 Vitest test files，包含原 Phase 0 測試 |
| `npm run typecheck` | 通過；亦核對無 `.next` 的乾淨副本可通過 |
| `NEXT_TELEMETRY_DISABLED=1 npm run build` | 通過；Docker production build 亦通過 |
| 後端 `pytest -m 'not network'`，`RUN_INTEGRATION=1` | **504 passed，2 deselected**；真實 PostgreSQL／Redis、隔離 schema，既有1項 Starlette deprecation warning |
| Compose | `config --quiet` 通過，四服務 healthy；`/health/ready` 的 PostgreSQL／Redis 皆 ok |
| 真實資料 proxy | TWSE 2330、TPEX 6488 的搜尋／latest 成功；canonical 數仍1973筆，沒有抓取或新增行情 |
| Chromium browser | **3 passed**；2330 搜尋／日週月／crosshair／zoom／pan、390px手機與768px尺寸變更、503重試／空資料無圖表；desktop/mobile 截圖已檢視 |

Chromium 使用工作區 `.tools/` 內的隔離 browser/runtime libs/中文字型，未變更系統套件。裸沙箱執行曾因 socket／subprocess／唯讀 telemetry 目錄受限而無法完成；後續以正常權限與 Compose 驗證成功。安裝後可用的瀏覽器測試指令（本機 tooling 路徑為忽略檔）：

```bash
PATH="$PWD/.tools/node/bin:$PATH" \
LD_LIBRARY_PATH="$PWD/.tools/browser-runtime/usr/lib/x86_64-linux-gnu" \
FONTCONFIG_FILE="$PWD/.tools/browser-runtime/fonts.conf" \
PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers" \
npm run test:browser --workspace @stockapp/web
```

首次瀏覽器嘗試因缺少 runtime libraries 無法啟動；準備隔離依賴後完成驗收。窄 K 棒調整後曾發現測試滑鼠取樣間距過大而錯過唯一 K 棒，縮小掃描間距並等待 animation frame 後，最終 **3項全部通過（6.1秒）**。

限制：現有資料庫每檔僅少量歷史行情，2330 圖表目前各週期只有一根真實 K 棒，完整 Calendar／歷史尚未核實；畫面已揭示此限制。漲跌與漲跌幅目前均為 null，因此數值呈現 `—`，等待後端未來有可核實參考價才可顯示。沒有執行外部交易所 network 測試或遠端 GitHub Actions；沒有 commit／push／部署或進入 Phase 1E。
