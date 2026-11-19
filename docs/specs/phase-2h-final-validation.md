# Phase 2H-7 — Broker Flow 完整驗證

驗證日期：**2026-11-19，Asia/Taipei**。Repository：`AlHIO/stock-analysis-platform`；本次基準為 `main`／`1256e9d`。本階段只執行驗收、補強有意義的資料保護測試及文件，沒有新增產品功能、正式匯入、部署、Commit／Push 或開始下一階段。

## 1. 結論與適用範圍

軟體驗收與正式來源驗收分開：

- **離線 Domain／Adapter／Import／Replay／API／UI：通過**。使用明確標記 SYNTHETIC 的固定資料與隔離 PostgreSQL 驗證；本次結果見下表。
- **正式分點資料數值與官方 TOP15**：**Blocked**。目前沒有已核實的專案下載、公開／衍生展示權或已驗收的官方實際報表，沒有用 Synthetic 值取代此驗收。
- **目前執行中的 Docker 分點功能**：**未啟用**。正式 Schema 仍為 `0010_daily_sync`，Broker 表未安裝，現有 Backend OpenAPI 沒有 Broker 路由。這是 Runtime 啟用缺口，與新版程式在隔離 PostgreSQL 的 API 測試分開。

因此，本報告不能解讀為「已取得正式主力進出」、「券商分點十年完整覆蓋」或「正式網站已上線」。既有 OHLCV、法人、集保、融資融券及借券資料也不能當作 Broker 覆蓋率。

## 2. 執行環境與保護

Python `3.12.16`、Node `22.23.4`、Next.js `16.4.5`、Playwright `1.64.1`；沿用既有依賴，未升級套件。

寫入測試由既有 `run_institutional_validation.py` 建立不同於正式庫的 `stockapp_p2b6_test_<random>` 臨時 Database，每個測試再使用私有 Schema。Migration Upgrade／Downgrade、Synthetic 匯入、並發與破壞性 SQL 測試都只在其中執行；Runner 結束後刪除臨時庫。

正式庫只使用 `REPEATABLE READ READ ONLY` 及 Timeout 查詢 Schema／少量歷史行情樣本。沒有正式 Alembic Upgrade／Downgrade、重啟 Docker／Worker、停止回補、修改來源旗標或執行下載。瀏覽器 Preview 僅綁定 `127.0.0.1:3100`，驗收後停止；Production Build 與 Preview 分開執行。

## 3. 本次實際測試結果

| 驗證項目 | 本次結果與範圍 |
| --- | --- |
| Pytest 離線 Unit／Regression | **3,301 passed**，9.11 秒；執行時排除 600 個 integration／network 項目 |
| Broker 子集 | 同條件 Collection 為 667 個離線案例；已包含於上述完整回歸，非另一次計算測試成果 |
| 完整 PostgreSQL／Migration Integration | **596 passed**，541.03 秒；3,305 個其他 Unit／Network 項目排除，臨時 DB 已移除 |
| 新增不可變事實 SQL 破壞矩陣 | **18 passed**，16.88 秒；另一個獨立臨時 DB 已移除 |
| OpenAPI Contract | Runtime Snapshot 測試包含於 Unit；`export_openapi.py --check` 通過 |
| Vitest | **428 passed／24 files**，1.93 秒 |
| TypeScript Typecheck | 通過 |
| Production Build | 通過，首頁／同源 Proxy／個股動態路由正常產出 |
| Playwright 全部 E2E | **26 passed**，35.7 秒；無失敗、無略過 |
| 現有 Docker／Health | Backend、Frontend、PostgreSQL、Redis、Daily Worker 皆 healthy；`/health` HTTP 200／ok |
| 正式 Broker Provider／官方數值／TOP15／頁面數值 | **Blocked／未執行**；未取得核准來源與實際報表 |

Pytest 的既有 Starlette／httpx deprecation warning，以及瀏覽器的顏色環境 warning，不影響通過結果；未藉此進行無關依賴變更。四個既有 Provider Network 案例不在本次離線驗收範圍，不列為通過。

本次 PostgreSQL／Migration 結果合計 **614 passed＝原有 596＋新增 18**，兩個 Runner 分別使用獨立臨時 DB；沒有把同一案例重複加總。

### 測試對照

| 要求 | 主要證據 |
| --- | --- |
| Domain Numerical | `test_broker_flow_domain.py`、`analytics_contracts`、`flow_calculations`、`proxy_calculations`、`engine`；固定數值、缺值、跨年、Decimal、As-of、來源／Scope 與版本 |
| Adapter Fixtures | `test_broker_flow_adapters.py`、`files`、`normalization`；平台具名欄位、UTF-8／BOM／Big5、CSV／ZIP 與拒絕不可信檔案 |
| Import／Replay／Resume | `test_broker_flow_ingestion.py`、`cli`、`ingestion_postgres`；Raw-first、逐列 Audit、冪等、舊 Raw、Revision、Checkpoint／Resume、失敗回滾及並發 |
| Migration／Constraints | `test_broker_flow_postgres.py`、`ingestion_postgres`；空 Schema Upgrade／Downgrade、Phase 1 舊行情保留、非空 Downgrade 拒絕、FK／Check／Scope 隔離 |
| 並發／不可變性 | `test_broker_flow_concurrency_postgres.py` 與新增 `mutation_postgres`；RC／RR 真正鎖等待、來源身分關閉、重複晉升、SQL UPDATE／TRUNCATE 防護 |
| REST／PostgreSQL HTTP | `test_broker_flow_api.py`、`query_postgres`；五個路由、精確字串、錯誤去敏、來源／Revision、單次 RR Snapshot、查詢成本及 GET 無 Provider／Raw／寫入 |
| OpenAPI | `test_openapi_contract.py` 與 Broker API Schema／Enum／Limit 測試；正式匯出 Snapshot 一致 |
| Frontend | `broker-api`、`broker-flow-panel`、`stock-chip-subpanels`、`chart-sync`、既有 StockChart／ChipChart 及 Proxy Vitest |
| Browser | `broker-dashboard.spec.ts`、`broker-real.spec.ts`、`chip-dashboard`、`chip-real`、`stock-chart`、`technical-fixture`、`technical-indicators` 全部七個檔案 |

上表的短名稱指 `apps/api/tests/test_broker_flow_<name>.py` 或 `apps/web/tests` 相應檔案；原始完整命令見第 8 節。

## 4. 金融數值與資料品質

以下全部是固定 **SYNTHETIC** 向量，不是官方買賣報表：

- V1 Buy／Sell 總數各 1,950，Net=0；有買進／有賣出家數差為 −1，淨買超／淨賣超家數差為 0。
- 相同 Net 用穩定 UUID Tie Break；正／負榜不以零值補滿，不在前端重排。
- V1 雙邊 Gross 分母 3,900；A Share=`28.205128205128%`、TOP5 Share=`94.871794871795%`、HHI=`0.234714003945` fraction。
- 跨年 V2：期間先彙總再 TOP N 的 Proxy=50；每日重選後累計=150。不同算法 ID／選擇方式不能互換。
- 選中分點聯集的實際 Buy／Sell 各 1,900；Top Positive Net=1,100，不把正／負 Net Magnitude 稱作實際買／賣量。
- 精確門檻相等可納入；`0.10000001` 的精度向量保留 Net Amount=`0.200000020000`，不經 float 或用均價乘 Net。
- 跨年五個確認 Session 的 TOP Net=4,500；隔離 PostgreSQL 另一組五 Session 累計=500、Buy Streak=3。
- 缺日、缺分點、未知欄位、不完整母體、Source／Scope／Unit 不相容、左端歷史截斷都保留狀態與空值，不補零或跳過缺日後宣稱完整。
- 未核實日曆不能以平日或最後 N 筆替代；未知發布時間保持 null／Provisional，未來觀察或匯入的 Revision 不提前使用。
- Raw／Revision／Canonical 分開。舊 Replay 不能回退新 Canonical；一份報表為原子更新，不拼接兩個版本。
- Broker 是交易中介，不能辨識個人、大戶／中實戶或真實庫存；沒有商品／日期 Multiplier，不提供假張數；週持股不日化。

### 不可變事實補驗

審查發現既有 `test_facts_immutable_to_sql_update_and_delete` 名稱涵蓋 UPDATE／DELETE，但本體主要驗證 DELETE；其他檔案只有代表性的 UPDATE。沒有發現實際資料破壞缺陷，但驗收敘述不能因此宣稱所有 SQL 形式都已驗證。

本次新增 `test_broker_flow_mutation_postgres.py`，以真實離線匯入程式在隔離 Schema 建立 Synthetic Facts，再對 9 張不可變事實表各執行 UPDATE 與 `TRUNCATE ... CASCADE`：Source Audit、Raw、Scope、Revision、Trade、Item Audit、Metric Definition、Import Group、Import Row。

18 個操作均被拒絕，檢查 SQLSTATE `23514`，而非把無權限或 FK 前置錯誤當成功。每次 Rollback 後比較完整快照，核對 Raw Bytes／SHA256 及 Canonical→Observation→Revision→Raw／Run 關聯未改變。Mutable Registry／受控 Identity Closure／Job Checkpoint 沒有被錯誤分類為不可變資料。

沒有因此修改 Migration、計算公式、Repository 或 UI。

## 5. Browser 結果與真實資料的區分

26 個 Browser 案例分類如下：

- **17 個 Fixture／攔截案例**：明示 Synthetic，驗證分點排行榜、完整／不完整視窗、單位、來源、Revision 不一致、Loading／Error／Empty／Partial／Stale、桌面／手機及圖表互動。
- **7 個未攔截的真實 Canonical 財務／圖表案例**：既有 TDCC、融資／借券、OHLCV、2016 K 線、歷史間隔及技術指標。只證明各實際樣本可讀，不能轉成 Broker 覆蓋。
- **1 個未攔截的 Broker 可用性案例**：真實 Instrument 存在，但目前 Docker 沒有 Broker 路由，驗證錯誤畫面及沒有假排行榜；不當作正式分點數值通過。
- **1 個混合案例**：模擬指標失敗，接著用真實 API 重試；不歸入純真實完整流程。

確認週資料只在真實統計日期出現，游標移到沒有該日觀察時留白；日期視窗不以跨頻率 Logical Index 對齊。拖曳／縮放、成交量開關、指標設定、Tab 保留及手機表格水平捲動均有驗證，瀏覽器沒有直接呼叫供應商。

保存 16 張截圖：6 張明示 `SYNTHETIC`、10 張 `real_saved`；分類見 `.tools/validation/phase-2h7/screenshot-manifest.json`。截圖中的 Synthetic 數字不能用作正式主力進出證據。

## 6. 來源權利與正式驗收 Blocker

本次核對現有 Repository／Manifest／契約範本；外部來源文件沿用 [H1 的 2026-11-13 查證](phase-2h-data-sources.md)，沒有重新查供應商條款、登入、購買、提交驗證碼或呼叫受限資料 Endpoint。

| 來源 | 本專案目前狀態 | 正式驗收缺口 |
| --- | --- | --- |
| TWSE BSR／授權商品 | `twse_licensed` disabled；四個權利／契約旗標皆 false | 適當權利、正式安全交付、實際 Body、Schema／版本、Scope、身分及完整性證據 |
| TPEx 公開／授權商品 | `tpex_licensed` disabled；四旗標皆 false | 同上；另需非可執行 CSV／ZIP，釐清 API Method／認證／絕對交付方式 |
| FinMind | 沒有已核准的 Broker Adapter／專案展示權 | 訂閱及預定使用／展示／衍生用途核可、實際樣本 |
| TEJ | Deferred，沒有核實的分點專用資料契約 | 商品／Table／Schema、歷史、權利與交付 |
| 平台離線交換格式 | `stockapp.broker-flow.offline.v1`／CSV／ZIP 已測試 | Repository Fixture 是 Synthetic；不是官方 TWSE／TPEx 原生格式驗收或正式使用權 |

`broker-offline-manifest-v1` 的 `download_enabled`、`contract_verified`、`public_display_verified`、`derived_display_verified` 均未開啟。`LicensedBrokerAdapter.fetch()` 拒絕下載；即使呼叫端替換旗標，也沒有可繞過的 HTTP 實作。

**Broker Provider HTTP Retry／Timeout／403／429 的真實管線驗收為 Blocked／不適用**，因下載尚未實作／啟用。Broker 已驗證的重試能力是手動 Replay／Resume，不能借用其他每日 Worker 的 Retry 宣稱完成 Broker HTTP 能力。

本次正式分點匯入 0 份；沒有官方 Buy／Sell／Net 對照、真實 TOP15 排名或正式分點 UI 數值驗收。正式 Broker 歷史範圍、發布 SLA、修訂／撤回順序、完整身分沿革也未驗證。供應商舊目錄起點不等於本平台持有資料或十年完整覆蓋。

## 7. 正式 Runtime 與資料保護

唯讀複查結果：

| 項目 | 結果 |
| --- | --- |
| Repository Alembic Head | `0012_broker_ingestion` |
| 正式 `alembic_version` | `0010_daily_sync` |
| 正式 Broker 表 | 0 張；不是從不存在的表查得零筆報表 |
| 執行中 Backend OpenAPI | 沒有 Broker Route |
| 真實 2330 UUID 的 Broker GET | HTTP 404，驗證錯誤處理，不宣稱有資料 |
| 現有 `/health` | HTTP 200／ok |
| 既有 Historical DailyBars Canary | 整合測試執行期間與結束後，5 筆內容／Source ID Hash 一致 |

Canary 是 2330 的 5 筆既有 **OHLCV**（2007-07-27、07-30、07-31、08-01、08-02），比較金融欄位與 Source ID 的 SHA256。這只是有限樣本保護檢查，不證明整個正式庫全量不變，更不是 Broker 歷史起點。測試寫入均限制在臨時庫，沒有以正式 Canonical 作 Seed。

兩次讀取時間為台灣時間 18:59:17（完整整合測試仍在執行）與 19:07:08（測試結束後），`transaction_read_only=on`；Schema／Broker 表數一致。Canary Hash 均為 `16591b9e3e9fbc7342d0ddc4d35f68a8e2f06b76c41001fa2d7e6af99586b7fa`，比對紀錄見 `runtime-comparison.json`。驗收後既有五個 Docker 服務仍為 healthy。

正式啟用仍須依 README 審查 H2／H3 Migration 並讓 Backend／Frontend 使用新程式。本次沒有執行這些正式操作。

## 8. 重現命令與證據

在 Repository 根目錄，使用既有 venv：

```bash
.tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration and not network' -q
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
npm test
npm run typecheck

cd apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/phase-2h7 -- \
  tests -m 'integration and not network' -q --tb=short
```

本次完整整合測試已啟動後才新增 18 個 Mutation 案例，所以另開臨時 DB 執行並分開計數：

```bash
# apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/phase-2h7 -- \
  tests/test_broker_flow_mutation_postgres.py -q --tb=short
```

Frontend 在 `apps/web`：

```bash
# 另一個終端先啟動只綁 localhost 的 Preview
BACKEND_URL=http://localhost:8000 npm run dev -- --hostname 127.0.0.1 --port 3100
PLAYWRIGHT_BROWSERS_PATH="$PWD/../../.tools/ms-playwright" \
  BROWSER_BASE_URL=http://127.0.0.1:3100 npm run test:browser
# Preview 停止後
npm run build
```

本機測試 Chromium 使用既有 `.tools/fonts.conf` 載入可顯示繁體中文的本機測試字型；無系統字型時，可額外設定 `FONTCONFIG_FILE` 指向合法本機 Fontconfig。沒有將測試字型作產品資產或對外發布。

本機忽略目錄 `.tools/validation/phase-2h7/` 保存 `pytest-unit.log`、`postgres-integration.log`、`postgres-mutation.log`、`vitest.log`、`typecheck.log`、`build.log`、`playwright.log`、`runtime-before.json`／`runtime-after.json`、截圖及分類。未將密鑰、DSN、原始正式檔案或環境設定放入報告。

本階段 Repository 變更只有本報告、README 連結及新增的 18 案例測試檔；沒有產品程式或 Schema 修正。Preview／Build 自動產生的 `next-env.d.ts` 與 `apps/web/AGENTS.md` 已還原／移除，不納入交付。

## 9. 下一步前置條件

正式分點驗收至少需要：專案適用的取得／保留／內部／公開／衍生用途權利、合法非執行 CSV／ZIP 真實報表、核實的原生格式與歷史版本、Broker／Branch 日期身分、真正統計日期與單位／Multiplier／Scope／完整母體證據、核實交易日曆及可追溯修訂。需要網路下載時，還須實作並驗收已核准的正式 Adapter、認證與 Retry／Rate Limit，而非單純把旗標改 true。

沒有歷史 Canonical 選擇時間線或核實發布資料時，不宣稱 PIT Backtesting 保證；目前 Price Reader 組裝與張數能力缺口也維持明示。取得必要條件後，再另行安排指定股票／日期的官方數值→Raw→Canonical→TOP15→API→畫面驗收。

本階段停止於驗證及報告，不自動修訂授權、啟用下載、部署或開發下一階段。
