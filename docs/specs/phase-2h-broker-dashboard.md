# Phase 2H-6 — 主力分點與 K 線副圖整合

實作／驗證日期：2026-11-19，Asia/Taipei。沿用既有 Next.js App Router、TypeScript、Tailwind／CSS Modules、Lightweight Charts 5.2.1 及 [H5 API](phase-2h-broker-api.md)。沒有重新建立平台、修改技術指標公式或開始 H7。

## 1. 已實作畫面

在 `/stocks/{instrument UUID}` 的既有籌碼 Dashboard 新增第五個「主力分點」Tab：

- Source／Scope 精確選擇；顯示來源名稱、交易口徑代碼／版本及說明，不合併不同來源。
- 1 日、5／10／20／60 個交易日、自訂區間；單日取真正已保存的報表日，自訂沿用上方籌碼日期範圍。
- TOP 15 買超／賣超分點：原始排名、當日有效分點名稱、買進、賣出、Net、來源數量單位及交易口徑。不補零分點或在前端重排排名。
- 明確命名「區間 TOP 15 分點淨額不平衡 Proxy」的方向／Net；使用後端的選中分點實際 Buy／Sell 數量。正 Net 不是買進量，負 Net Magnitude 不是賣出量。
- 活躍買進減賣出家數，以及淨買超減淨賣超家數；分開展示，不互相替代。
- 指定分點及期間累計；5／10／20／60 交易日委派 H4，真正區間與完整度保留。
- 每日重選 TOP N Proxy、家數差圖、Source／Scope、統計日、發布／首次觀察／匯入、Revision、Formula／Algorithm Version 及權利限制。

張數欄保留 Not Available 與原因：現有 API 沒有經核實的商品／日期 Multiplier，不自行假設一張 1,000 股。分點流量不能解讀為特定投資人、真正中實戶／大戶或實際庫存變化。

Loading、Error、Empty、Partial、Not Verified／Unavailable 均有獨立呈現；切換選擇時不將上一組數字重新標成新結果。Source 沒有核實發布 SLA 時為時效未核實，不因日期較舊而自行判定 Stale。若後端已有 Stale／Delayed 狀態，沿用該狀態。

## 2. K 線與副圖

保留既有 SMA／EMA／RSI／MACD 引擎與設定，價格圖新增成交量顯示開關。在價格圖下方使用同一 `ChipChart` 加入可切換副圖：

| 副圖 | 資料與可調參數 | 單位／頻率 |
| --- | --- | --- |
| 法人買賣超 | 外資、投信、自營商；既有 Derived Daily 結果 | 股／單位，日 |
| 分點 TOP N Proxy | 每日重選 TOP 5／10／15／30，精確來源 Scope | 股／單位，日 |
| 分點家數差 | 活躍家數差／淨方向家數差 | 分點，日 |
| 集保大戶比例 | 400,001／1,000,001 股起的既有後端門檻 | %，原生週統計 |
| 融資／融券 | 兩個 Ledger 擇一，不加總餘額成期間流量 | 股／單位，日 |
| 借券賣出 | 單一 Channel 的當日賣出量 | 股／單位，日 |

副圖以價格查詢範圍讀取，與 Dashboard 獨立的籌碼區間分開。每次副圖最多 366 個日曆日；長期價格圖仍可保留，副圖提示縮短期間，不暗中截取最近一年冒充原區間。

數量、價格、家數、百分比使用獨立圖表及清楚單位，不共用混淆尺度。正負 Histogram 分色；來源零保留零，缺值為 Whitespace。既有轉換器將精確字串轉成顯示用數值，只供像素位置，Tooltip／標籤保留 API 精度；不可安全繪圖的數值保持明確錯誤，不偽造近似金融值。

### 日期同步與視窗

`chart-sync.ts` 是每個 Instrument 的互動 Registry。`StockChart`／`ChipChart` 的 `syncGroup` 相同時，傳遞實際 ISO 統計日期與 Calendar-time Visible Range，不以 Logical Index 對齊不同頻率。

- 目標圖只在確實存在該日觀察時顯示值；沒有該日的週資料就顯示無觀察／清除游標，不使用最近一週數字冒充當日。
- 不插值、Forward Fill 或建立新 Data Points；主圖週／月 K 也只使用原有實際 Bar Date。
- 程式設定游標／視窗時抑制回音，避免圖表互相觸發無限同步。隱藏圖不發布互動事件。
- ResizeObserver 忽略隱藏時的零尺寸；清理 Chart、Observer、事件與 Registry 註冊。
- 開關成交量／技術指標時保存價格圖 Viewport；Tab／副圖隱藏保留元件與設定，新副圖接上現有日期視窗，不主動重設價格圖。

## 3. 前端架構與安全

新增 `broker-types.ts`／`broker-api.ts`，由既有 `chip-types.ts`／`chip-api.ts` 匯出。Client 與 UI 分離，重用既有 Request／ApiError；只透過 `/api/market/...` 同源 Proxy 存取 FastAPI，不直接請求 TWSE／TPEx／TDCC。

Client Guards 驗證 Instrument、原樣 Source／Scope、日期、Request Parameters、Session Window、金融字串、Revision 與完整度。未知／矛盾回應不顯示。History 有界分頁，不默默下載全部；預覽有截斷時提示其不是完整覆蓋。

Proxy 僅加入既定的 Broker GET Allowlist，固定上游及 Timeout；不接受外部 URL、POST 匯入或供應商路由。Broker Metrics 的參數 JSON 上限 4,096 字元，編碼後 Query 上限 16,384；其他查詢維持原本 2,048。錯誤只保留核准代碼，去除內部訊息。

Visited Tabs 與開啟過的副圖保持 Mounted，設定屬各元件本地 State，不導入全域金融狀態。每個請求有 AbortController、Selection Key 與結果歸屬，切換範圍或來源取消舊請求，避免舊回應覆蓋新選擇。

買榜、賣榜與指標是多個 HTTP 請求，因此額外比較統計日／Source／Revision／Contract／Scope 的輸入集合。集合不同時不將它們當成同一版分析，相關摘要／圖表停用並提示重新核對；按「重新核對本區間」保留來源、期間與分點設定。此比對不是金融計算，也不宣稱跨請求 Snapshot 保證。

## 4. 必要的後端契約補充

H5 缺少兩項 UI 必要資料，因此作向後相容補充，沒有新表或 Migration：

1. TOP API 可選 `sessions=1/5/10/20/60`。Application 重用 H4 `branch_session_cumulative_net` 的核實日曆視窗選擇，再以同一 H4 TOP 排名算法計算；沒有第二套交易日或金融公式。`details.session_window` 保留 requested_sessions、verified、原查詢日期與真正 Expected Dates。未核實、缺 Session、歷史不足時 Value 為 null。
2. 區間 TOP N Proxy 的 Details 增加 `selected_buy_quantity`、`selected_sell_quantity` 及 `selection_basis=union_of_top_net_buyers_and_sellers`，由既有 Domain Branch Aggregates 精確加總選中聯集的 Side Quantities。原 Proxy Net、選擇方式及算法版本不變，不把 Net Magnitude 當實際 Side Quantity。

前端只將伺服器回傳的已核實 Session Window 起訖複製到後續同區間 Proxy 查詢，不自行計算最近 N 個平日或取 N 筆。資料不足時不退回較長日期範圍卻繼續標成 N 日結果。

固定 Synthetic 測試包括跨年五個確認 Session Net=4,500、無核實日曆的全部 N 控制項為 Not Verified，以及 V1 選中實際 Buy／Sell 各 1,900、Top Positive Net=1,100 的語意區別。PostgreSQL HTTP 另驗證同一五交易日視窗與 Net=500。

## 5. 測試與操作

所有 Synthetic Fixture 僅在 `tests/fixtures`／Browser Route Interception 使用，正式 Components 不匯入 Fixture，不建立正式 Seed。

```bash
# Repository 根目錄
.tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration and not network' -q
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
npm test
npm run typecheck

# 私有 Schema＋臨時 DB，runner 結束後刪除臨時 DB
cd apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/phase-2h6 -- tests/test_broker_flow_query_postgres.py -q --tb=short

# 在 apps/web，先於另一個終端啟動本機 Next 預覽
PLAYWRIGHT_BROWSERS_PATH="$PWD/../../.tools/ms-playwright" npm exec -- playwright install chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/../../.tools/ms-playwright" BROWSER_BASE_URL=http://127.0.0.1:3100 \
  npm run test:browser -- broker-dashboard.spec.ts broker-real.spec.ts chip-dashboard.spec.ts stock-chart.spec.ts
# 停止預覽後再執行
npm run build
```

以下為本次實際執行結果：

| 檢查 | 實際結果 |
| --- | --- |
| Vitest | 428 passed，24 test files |
| Backend Unit／既有回歸 | 3,301 passed；600 integration／network deselected |
| 隔離 PostgreSQL Broker HTTP | 27 passed；22.25 秒，臨時 DB 已刪除 |
| Typecheck | 通過 |
| OpenAPI Snapshot | Export --check 通過 |
| Production Build | 通過，既有首頁／Proxy／個股動態路由成功產出 |
| 新增 Desktop／Mobile Synthetic E2E | 7 passed，包含週資料精確日期十字線與各種 UI 狀態 |
| 舊 K 線／Dashboard E2E | 15 passed，包含 2 項既有 TDCC／融資／借券真實 Canonical 比對；合併共 23 passed，21.0 秒 |
| 真實分點服務不攔截驗證 | 1 passed（錯誤狀態驗證）；現有 Backend 回 404，未聲稱正式分點數值驗收 |

本機記錄位於忽略目錄 `.tools/validation/phase-2h6/`。初次瀏覽器執行因缺少相容 Chromium 未啟動；補裝測試 Binary 後重新執行，不把環境失敗宣稱產品驗收通過。測試 Chromium 原本缺少 CJK 字型，使用本機 Windows 已安裝的 Noto Sans TC 測試副本及獨立 Fontconfig 檢查中文排版；沒有發布字型或修改系統。桌面／手機截圖檔名包含 SYNTHETIC，不當作正式資料成果。沒有無關套件升級。

主要變更為 `broker-flow-panel`／`stock-chip-subpanels` 元件及樣式、`broker-api`／`broker-types`、`chart-sync`，以及既有 Dashboard／StockChart／ChipChart／IndicatorWorkspace／StockDetail／同源 Proxy 的整合。新增／更新 Unit Tests、Synthetic Fixtures 及兩個 Broker E2E 檔。後端修改限 TOP Session Window、Proxy Side Metadata、API／PostgreSQL 固定測試與 OpenAPI Snapshot；沒有模型、DDL 或依賴版本變更。

## 6. 真實覆蓋與未支援事項

本階段沒有取得、下載或匯入正式分點報表；沒有正式 Migration、修改行情或既有籌碼資料、重啟 Docker／Worker 或公開部署。

無攔截驗證使用已保存的真實 2330 Instrument UUID；同源 Broker History 實際回 404／`instrument_not_found`（目前執行中的 Backend 尚未提供新路由），畫面呈現查詢錯誤而非假排行榜。記錄在 `real-runtime.json`，其中 `real_broker_coverage_validated=false`。此結果驗證錯誤處理，不代表 H5／H6 已於正式 Docker 上線。

- H1／H5 的正式下載、公開／衍生展示權限制保持 Blocked；Internal Canonical Query 不等於公開展示核可。
- 正式核實交易日曆尚未接入，5／10／20／60 交易日完整排名／累計不能因數筆資料存在就宣稱完整。
- 沒有商品／日期 Multiplier，因此不提供假張數；沒有真實中實戶或個別投資人辨識。
- 價格與籌碼各自讀既有 API 做真正日期對照；不估分點成本、庫存、獲利或因果關係，也不產生無公式的主力信心分數。
- 沒有十年完整分點或每日大戶持股資料；集保仍是原生週統計。
- 本機執行中的 Docker 映像不因原始碼修改自動更新。要使用新畫面及 API，需依 README 審查既有 H2／H3 Migration 並重建相應服務；本次未自行部署。

交付停止於 H6，不開始 H7、Commit 或 Push。
