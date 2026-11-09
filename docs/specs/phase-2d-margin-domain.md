# Phase 2D-1 — Margin Domain & Source Contracts

完成範圍：純 Domain、計算服務、Ports、版本化來源能力與 synthetic 固定數值測試。驗證日期：2026-11-09，Asia/Taipei。本次沒有新增資料表、Migration、HTTP Adapter、Normalizer、匯入工作、CLI、API 或前端功能，沒有匯入任何融資融券資料。

依據：[Phase 2A 來源查證 §5](phase-2a-data-sources.md)、[Domain 草案 §3.3](phase-2-domain-model.md)、既有 institutional／ownership Domain。最新任務將雙 Ledger 與公式測試納入 2D-1；Roadmap 已同步，Persistence／CLI／API 留在後續 2D-2。Phase 2A 的通用 buy／sell／repayment API 草案，本次以語意明確的內部欄位實作，尚未新增公開 API。

## 模組與邊界

```text
modules/margin/
  domain/
    models.py         數量／缺值、雙 Ledger、時間、限額、換算證據、報表與 History
    calculations.py   唯一的 MarginCalculator
    results.py        Decimal 結果、完整度、公式／算法及來源參照
    sources.py        Request／Snapshot DTO、欄位／日期／能力契約
    ports.py          Reader、Repository、Source Adapter 與 Calculation Protocols
  infrastructure/
    source_manifest.py  官方端點、欄位對照、授權證據與限制；沒有 I/O 實作
```

Domain 只依賴標準庫及自身模組，不 import FastAPI、SQLAlchemy、Redis、HTTP client 或供應商 SDK，也不直接讀其他模組私有資料表。未來 Application 經 Ports 使用 Infrastructure；本次沒有建立空服務或更改既有行情／法人／集保公式。

公開 Ports：

- `MarginHistoryReader.load`：有限範圍、同一證券／市場／來源／契約／Schema／scope／unit 的歷史資料。
- `MarginRepository.get/list_revisions`：未來讀取完整報表 revision；本次沒有寫入或 promotion 實作。
- `UnitConversionReader.resolve`：以證券、市場、日期與來源／目標單位取得已審查換算證據，沒有證據回 None。
- `MarginSourceAdapter.fetch`：未來傳回原始 bytes Snapshot；請求日期與擷取時間不能代表資料日期。
- `MarginCalculationEngine`：共用餘額核對、額度與換算算法，後續 API 或分析不得另寫公式。

## 數量與缺失

`Quantity` 保留 `value: int | None`、`unit: shares | lots | units` 與 `availability`：

| 狀態 | value | 意義 |
| --- | --- | --- |
| reported | 包含 0 的明確整數 | 來源確實回報；0 是有效觀察 |
| missing | null | 未提供、無法確認或來源缺欄 |
| not_applicable | null | 來源契約明確說明不適用 |

數量依 Phase 2A Domain 使用嚴格 int／BIGINT，拒絕 bool、float、字串、Decimal 冒充整數、超界數值。Ledger 的餘額、買進、賣出、償還與限額不可負；只有 Signed Adjustment 可以帶正負值。金額不是股數，本次未建立融資金額模型；未來必須另帶 Decimal／currency，不能把市場 summary 仟元塞進數量欄位。

報表保留來源單位，兩個 Ledger 及同份報表限額必須一致。LOTS 可供原始語意與計算測試使用；宣告 canonical 可用前必須有適用的 SHARES 或 UNITS 換算證據。本階段沒有寫入 canonical。

## 雙 Ledger 與公式

`FinancingLedger` 與 `ShortMarginLedger` 分開，不能交換買賣方向，也不能將每日餘額加總當成交量。

| Ledger | 已核實 base identity | Synthetic 固定值 |
| --- | --- | --- |
| Financing | previous_balance + purchase − sale − cash_repayment | 100 + 20 − 7 − 3 = **110** |
| Short margin | previous_balance + short_sell − short_cover − stock_repayment | 50 + 9 − 4 − 2 = **53** |

融券 short_cover 指官方買進還券；stock_repayment 指現券償還，兩者分開。TPEx 官方欄位拼字 `ShortConvering` 對應 short_cover，不改成其他猜測欄位。`Offsetting`／資券互抵保留為來源欄位契約，沒有再次加到任何 balance identity。

`FormulaPolicy` 帶版本、verified 與 adjustment_treatment：

- `none`：已核實公式沒有 adjustment；對應 Quantity 必須明確 not_applicable，不能用 missing 或補 0。M1／M2／M5 均採此政策。
- `signed`：只有另有核實版本契約時才能將 observed signed_adjustment 加入公式。Synthetic 測試 +5／−6 分別使融資期望餘額為 115／104；這不是宣稱官方 M1／M5 存在調整欄。
- `unverified` 或 verified=false：回 not_verified，不套用猜測公式。

缺少任一必要 flow／balance 時不補零，回 missing_data；必要欄明確不適用回 not_applicable。公式不符回 invalid，value=null，保留 expected、reported、difference=`reported − expected`。引擎不修正來源值或挑另一個數字使公式通過。

算法版本 `margin-calculator-1.0.0`。衍生與換算採獨立 Decimal Context，precision=50、ROUND_HALF_EVEN，Inexact 觸發錯誤，**沒有實際捨入步驟**。結果是精確 Decimal，數量保持整數語意；不受呼叫端全域 Decimal precision／traps／exponent 設定影響。換算證據 factor 是有限正 Decimal、符合 NUMERIC(32,8) 精度，不由 float 轉換。

## 換算證據

`ConversionEvidence` 必須帶 instrument UUID、market UUID、source_unit、target_unit、factor、valid_from／valid_through 及 evidence_version。目前只接受 LOTS → SHARES／UNITS，不需要換算時直接保留已核實原單位，沒有通用 1,000 倍預設。

`convert_quantity` 同時核對身分、市場、來源單位及有效期間；結果必須是可放入 BIGINT 的完整股數／單位數。Fractional 結果與 overflow 回 incompatible_data，不四捨五入。缺少 multiplier 屬無效輸入，不能產生 canonical 數量。

Synthetic 測試 103 張以 factor=1,000 得 **103,000 股**，factor=100 得 **10,300 股**；factor=0.5 得 51.5 股時拒絕。實際商品乘數仍須後續取得官方、具有效期間的證據，本次未建立證券換算資料庫。

## 來源時間、Revision 與相容性

`SourceTiming` 分開保存 trade_date、date_evidence、date_evidence_version、nullable source_published_on／source_published_at、first_seen_at 與 timezone。只知發布日期時維持 timestamp=null，不補午夜；兩者同時有值須依來源時區一致，發布時間不能晚於首次觀察。

- 可保留 undated／unverified timing，但不能構成 `MarginDailyReport`。
- 來源明確欄位用 source_field；無日期來源須有已審查的 verified_payload 證據。
- verified_payload 必須帶日期證據綁定的 payload SHA256；報表會核對其與 lineage payload hash 相同，不能挪用另一份快照日期。
- request date、下載日期、HTTP Last-Modified 或另一個資料集的日期均不是可信日期證據。
- 交易日以來源市場本地 date 保存；不能以 UTC 截日取代。晚於 first_seen 當地日期的交易日拒絕。不從現有資料猜測完整交易日曆。

`MarginSourceLineage` 保留 source/run/raw/revision UUID、row locator、payload SHA256、contract／schema／parser／identity evidence／formula／promotion policy 版本。計算結果保留完整 lineage、timing、兩個 FormulaPolicy 及 algorithm version。

`MarginHistory` 拒絕重複日期，並要求 instrument、market、source、contract、schema、parser、dated identity evidence、scope、quantity unit、formula／promotion policy 及兩個 Ledger FormulaPolicy 完全相容；revision/run/raw 可不同。它不接受跨來源或不同交易口徑直接合併。

D+1 的 previous_balance 是 D+1 來源的獨立 assertion，可能因更帳與 D 初版 balance 不同。例如 D current=110、D+1 previous=108 可同時保留，不自動覆寫、拼接或判定任一版本損壞。Revision promotion、資料保存與 transaction 留待 Persistence 階段。

## 限額與可用額度

`QuotaLimit` 保存來源限額，**不是**宣稱來源已提供 Available Quota。保留 ledger、quantity、report_date、effective_on、effective_relation、basis 及 evidence_version：

- report_date 是報表統計日，effective_on 是限額生效日。
- 只知次一營業日時保存 next_business_day 與 effective_on=null，不用日曆日 +1。
- `available_quota` 只有在相同 Ledger／單位、經證據核實的 balance_ceiling basis、明確同日生效與已回報 balance 下，才計算 `limit − balance`。
- Unknown basis、未知／次日生效回 not_verified；缺限額回 missing_data。已回報 balance 可以獨立計算額度，不需要拿當日 flow 代替。
- Synthetic limit=200、融資 balance=110 得 **90**；limit=110 得真正的 **0**。limit=100 時保留診斷 headroom=−10，回 incompatible_data/value=null，不 clamp 為 0，也不使原始報表失效。

官方尚未證明各限額欄都是當日 balance ceiling，因此 Manifest 沒有替來源預設此口徑。沒有 `sold ≤ limit` 的通用限制，也不將衍生結果保證為可實際下單的額度。

## Source Capability Manifest

版本 `margin-sources-1.0.0`。所有來源 `default_network_enabled=false`，本次沒有下載實作；Capability、授權與實際啟用是不同條件。Schema 契約保留欄名與單位，依名稱檢查，不依固定位置，缺欄／未知欄／重複欄均拒絕。

| 來源 | Method／實際 Endpoint 與參數 | 日期／頻率／單位 | 使用權、能力及 gate |
| --- | --- | --- | --- |
| M1 TWSE | GET `https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN`；無參數 | 無 Date；daily；quantity 張 | OpenAPI 平台有 OGDL 連結；latest-only。16 個字串欄位，缺可信日期及商品換算證據時阻擋 |
| M2 TWSE dated | GET `https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN`；date=YYYYMMDD、selectType=ALL、response=json | 回應 date 西元；daily；證券張／市場 summary 另有仟元 | 網站自動利用／轉載權 pending；只實測 2016-11-07，官方頁宣告 2001-01-01 起不代表連續覆蓋。群組欄位契約未核實，column_contract_verified=false，保持阻擋 |
| M5 TPEx | GET `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance`；無參數 | Date 民國 yyyMMdd；daily；quantity 張、utilization % | Dataset 11387 免費／OGDL1；latest-only。21 個欄位；明確拒絕所有指定日期請求，不宣稱歷史 API |

M1/M5 Swagger 宣告 object、Phase 2A 實測 body 為 JSON array；Manifest 記錄此差異，沒有假稱本階段已實作或驗證 Parser。M2 目前只有語意欄位對照，不把它冒充可直接解析的 flat literal Schema。M3／M4／M6／M7／M8／M9 在 Manifest 明列 deferred／其他產品，借券賣出、借券未還、每日 short flow 不代填 financing balance。

官方發布精確時刻與 Rate Limit 未核實，Manifest 以 null 保留；沒有捏造刷新 SLA 或「每秒 N 次」。未來 I/O 必須另有 timeout、有限重試及保守節流，但本次沒有可執行下載功能。

官方依據：

- [TWSE Swagger](https://openapi.twse.com.tw/v1/swagger.json)：2026-11-09 僅重查 metadata，M1 16 欄、無 Date／參數；沒有下載行情報表。
- [TPEx Swagger](https://www.tpex.org.tw/openapi/swagger.json)、[TPEx Dataset 11387](https://data.gov.tw/dataset/11387)：沿用 Phase 2A／2026-11-08 metadata 查證；資料集仍連 legacy CSV 並引用現行 Swagger，不宣稱直接指向相同 body。
- [政府資料開放授權條款](https://data.gov.tw/license)、[TWSE 使用條款](https://www.twse.com.tw/zh/terms/use.html)、[TWSE 融資融券官方頁](https://www.twse.com.tw/zh/trading/margin/mi-margn.html)。

## 驗證

固定值皆明確標記 **SYNTHETIC**，只用於測試，不寫正式資料庫。Domain 可單獨測試，無 Docker、PostgreSQL 或外部網路依賴：

```bash
.tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_margin_models.py \
  apps/api/tests/test_margin_calculations.py \
  apps/api/tests/test_margin_source_contracts.py -q

RUN_INTEGRATION=0 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest \
  apps/api/tests -m 'not integration and not network' -q --tb=short

PATH="$PWD/.tools/node/bin:$PATH" npm test
PATH="$PWD/.tools/node/bin:$PATH" npm run typecheck
PATH="$PWD/.tools/node/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 npm run build
```

涵蓋雙 balance identity 數值、正負調整、缺值／不適用／零、溢位與 Decimal 精度、乘數與有效期間、時區／來源日期／payload 日期證據、限額不同生效日、重複日期、不相容來源、D+1 更帳及 Domain import 邊界。

2026-11-09 實際結果：

| 檢查 | 結果 |
| --- | --- |
| Margin 固定值／契約單元測試 | **218 passed**（Models 95、Calculator／import 邊界 70、Source Contracts 53） |
| 全部不連網、非整合 Pytest | **1,754 passed、386 deselected**；含 Phase 1、2B、2C 回歸 |
| Vitest | **244 passed、15 files** |
| TypeScript Typecheck | 通過 |
| Next.js Production Build | 通過，既有動態路由正常產製 |
| Fresh Python process 只 import Margin Domain／Ports | 通過；未載入 FastAPI／SQLAlchemy／httpx／Redis／psycopg |
| `docker compose config --quiet` | 通過 |
| Docker Compose 唯讀健康查詢 | backend／frontend／postgres／redis 均 healthy |
| `git diff --check` | 通過 |

沙箱內完整 Pytest 無輸出，已中斷該次測試並於沙箱外成功重跑；沒有操作 Docker Worker。Build 沙箱內第一次因 TypeScript 子程序輸出為空失敗，沙箱外相同建置指令通過，未修改套件或前端設定。建置自動產生的 next-env.d.ts 已還原。Pytest 有一項既有 Starlette TestClient deprecation warning，未為此升級依賴。

386 deselected 是明確排除的 integration／network 測試，**不當作通過**。本階段沒有 Schema、Repository 或 HTTP I/O 實作，所以未執行 PostgreSQL 寫入、Migration 或官方資料匯入驗收。沒有操作正式資料或啟停服務，Source Manifest 所列能力不代表已取得真實融資融券覆蓋。

## 後續限制與 2D-2 待辦

1. 取得各商品、有效期間的官方張股乘數及 dated Instrument identity 證據。
2. 解決 M1 同份 payload 的可信日期；M2 權限及群組 Schema 未核實時維持停用。
3. M5 只支援官方最新 snapshot；歷史 machine endpoint、完整覆蓋與刷新時刻仍未核實。
4. 核實 quota 的實際 basis、生效日期、註記作用及可用額度定義；不能從本次 synthetic 測試推定正式來源可用。
5. 經下一階段授權後才建立 typed raw／audit／revision／canonical、交易邊界、promotion policy、Adapter／Normalizer／CLI 與 API，並進行隔離 PostgreSQL 與合法官方來源驗收。本次停止於 2D-1。
