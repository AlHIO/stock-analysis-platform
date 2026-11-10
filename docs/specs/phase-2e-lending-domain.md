# Phase 2E-1 — Securities Lending Domain & Source Contracts

狀態：純 Domain、計算服務、Public Ports、版本化 Source Manifest 與 synthetic 固定數值測試。驗證日期：2026-11-10，Asia/Taipei。本次沒有新增正式資料表、Migration、Repository 實作、HTTP Adapter、Parser、匯入 CLI、API、排程或前端；沒有匯入任何借券資料，也沒有重啟 Docker／歷史回補 Worker。

依據：[Phase 2A 來源 §5](phase-2a-data-sources.md#5-c--融資融券與借券)、[Domain 草案 §3.3](phase-2-domain-model.md)、[Roadmap 2E](phase-2-roadmap.md#6-phase-2e--借券與借券賣出)。Phase 2A 模組表曾將 Lending 列在 margin，本次依最新任務建立獨立 lending 模組；既有 margin／institutional 公式及 Schema 沒有改寫。草案的「M4 初版僅啟用 SLBNLB」在本階段解讀為來源契約選項限制，並非已取得使用權或已啟用下載。

## 檔案與架構

```text
modules/lending/
  __init__.py
  domain/
    __init__.py
    models.py        數量、雙 Ledger、Channel／Partition、Quota、時間、Lineage、History
    calculations.py  唯一 LendingCalculator
    results.py       精確結果、公式／版本及完整輸入參照
    sources.py       Request／Snapshot、分組欄位、產品／日期／Schema／Channel 契約
    ports.py         Reader、Repository、Source Adapter、Calculation Protocols
  infrastructure/
    __init__.py
    source_manifest.py  版本化來源證據與限制；只有不可變設定，沒有 I/O
```

Domain 僅依賴標準庫與自身模組，不 import FastAPI、SQLAlchemy、Redis、HTTP client、供應商 SDK 或其他模組私有公式。未來 API → Application → Domain；Infrastructure 實作 Ports。本次沒有為未授權的下一階段建立空服務。

Public Ports：

- `LendingHistoryReader`：以 UUID／市場、日期範圍、kind、來源契約、scope、unit、channel family／code 讀一個可比較序列。
- `LendingRepository`：查詢完整報表／Revision，不隱式 commit，沒有實作或資料表。
- `LendingSourceAdapter`：未來依 typed Request 取得原始 bytes Snapshot；請求日期不代表回應統計日。
- `LendingCalculationEngine`：共用兩種 balance identity、Channel 合計及對帳。

## 四種事實不可混用

| 事實 | 模型／欄位 | 語意 |
| --- | --- | --- |
| 借券未還餘額 | BorrowingLedger.balance | 已借入、尚未返還的 stock |
| 當日新增借券／還券量 | borrowed_quantity／returned_quantity | 每日 flow，不是未還總量 |
| 借券賣出餘額與流量 | LendingShortLedger.balance／short_sales／short_returns | 已借券並賣出未回補的 stock，以及當日賣出／還券 flow |
| 法規限額／可用額度 | RegulatoryQuota | 獨立來源 assertion，不是成交、借券量或餘額 |

`ReportKind` 僅有 borrowed_positions／lending_short_sales，分別強制搭配 BorrowingLedger／LendingShortLedger。Quota 不建立假 Ledger。SourceProduct 另列 daily_short_flow／quota_only，使只提供流量或額度的來源不能冒充 balance report。

`Quantity` 保留嚴格 int／BIGINT、unit、availability。reported=0 是有效觀察；missing 與 not_applicable 都保留 null 且意義不同。拒絕 bool、float、Decimal 或字串冒充整數；餘額／新增／返還／限額非負，只有 signed_adjustment 可正可負。來源 unit 可描述 shares／units／lots；本階段正式 LendingDailyReport 僅接受 shares／units，沒有任何預設張股換算，也不把金額欄重命名為數量。

## 精確公式與結果

| Calculator 方法 | 唯一公式 | Synthetic 固定值 |
| --- | --- | --- |
| borrowing_identity | previous_balance + borrowed_quantity − returned_quantity | 1000 + 240 − 90 = **1150** |
| lending_short_identity | previous_balance + short_sales − short_returns + signed_adjustment | 400 + 125 − 80 − 7 = **438** |

借券未還 M4 已核實 base formula 沒有調整項，政策為 verified／NONE，欄位必須明確 not_applicable，不能補 0。未核實的 Borrowing SIGNED 政策可以保留原始調整事實，但 v1 引擎回 not_verified，不自行增加公式。

借券賣出公式使用 verified／SIGNED；明確調整 0 合法，−7 得438、+7得452。Missing Adjustment 回 missing_data、value=null，不拿 0 代替；不符合該 kind 的公式政策一律 not_verified。新版來源公式須先有明確版本與數值測試，不能把融資／融券公式拿來代用。

Known 公式矛盾時回 invalid，保留 expected、reported、difference=`reported − expected`；value=null，不修改來源數字。缺少必要欄回 missing_data；明確不適用但該公式需要的欄回 not_applicable。空集合不是 0。

數量來源使用 int；運算與輸出使用 Decimal。算法 `lending-calculator-1.0.0`，獨立 Context precision=50、ROUND_HALF_EVEN，Inexact 觸發錯誤，沒有浮點或隱式捨入。大於 2^53 的輸入與高於單筆 BIGINT 的衍生合計仍保持精確，不受呼叫者全域 Decimal precision／traps 影響。

CalculationResult 帶 formula、algorithm_version、精度、完整 CalculationInput（UUID、日期、kind、unit、Channel、SourceTiming、Lineage、FormulaPolicy）及 ChannelPartition 證據與版本。新公式只在 lending engine 存在，不複製進未來 API／UI。

## Channel identity、合計與重複保護

`ChannelDefinition` 分開保存 channel、code、family、definition version、verified 及明確 leaf coverage：

- combined 的官方代碼為 SLBNLB；slb／nlb 對應 SLB／NLB。
- other_verified 必須帶自己的 code；不把 LON／TWTBBU 自動合併成同一序列。
- unspecified 不得宣稱 verified 或 coverage；M6 的來源 Channel 無法確認時採此值，不猜成 combined。
- 已核實 SLBNLB 是合計名稱，不代表已確認 SLB／NLB 構成完整、互斥的數學分割。Coverage 留空表示未知，不能當成空集數量 0。

`ChannelPartition` 是額外、已審查的分割證據：source UUID、contract／taxonomy、kind、scope、unit、有效期間、family／definition version、total code、完整 member codes、total leaf coverage 及 evidence version。本階段 **沒有提供任何官方 verified partition 常數**；測試 proof 明確標為 synthetic。

`aggregate_balance` 只有在以下全部成立才回傳合計：

1. 恰好包含所有 member，沒有重複、未知成員或 Combined total。
2. 同一證券、市場、交易日、kind、來源與完整可比較 profile；符合 proof 的來源／日期／單位／scope／版本。
3. Member Channel 及 coverage 已核實，leaf 集合互斥且 union 精確等於 total coverage，沒有缺子系列。
4. 同一 Raw UUID／Run UUID／payload hash，且 publication on／at 相同；僅發布 timestamp 相同不足以證明沒有修訂。本版不合併跨 Raw parts，未來若有合法需求須先建立 reviewed release-binding contract。
5. 各成員自己的 balance identity 都完整且正確；缺 flow 或錯誤 balance 不會被總數掩蓋。

此合計只針對**同一交易日 stock balance**，不能把不同日期每日餘額相加當期間借券量。

`reconcile_channels` 比較 reported combined balance 與上述成員合計，不再把 total 加一次。Synthetic SLB=570、NLB=340 得910，reported combined=910 residual=0；total改為911但自身identity仍成立時回 invalid/residual=1。Combined＋SLB＋NLB 不會算成1820。借券賣出子系列438＋67=505的帶符號調整案例也獨立測試。

## 日期、發布、Quota 與版本

`SourceTiming` 保存市場所在地 trade_date、date evidence／version、first_seen_at、nullable source_published_on／source_published_at 與 timezone。禁止以下載日、request date、UTC 截日或 HTTP header 代替統計日。只有日期的發布資訊不補午夜；同時給發布日期與時刻時須依來源時區一致。來源日期晚於首次觀察當地日期時拒絕。

已審查的 payload date evidence 必須綁定 SHA256，報表再核對與 Lineage hash 相同。Undated／unverified timing 可描述尚未核實來源，但不能構成 LendingDailyReport。

RegulatoryQuota 保存 purpose、Quantity、report_date、effective_on、same_day／next_business_day／unspecified、basis code 及 evidence version。未知生效日維持 null，不用 D+1 曆日推測營業日。Standalone undated quota 是值物件，還不是可入庫的正式觀察；本次沒有建立其 Persistence。

不做通用 `short_sales ≤ quota` 檢查，不從 quota 算實際流量或與其他 Channel 額度直接相加。Synthetic sold=125、quota=1 仍可符合既有 balance identity；這不表示可實際下單，只是不套用來源未定義的限制。

Lineage 保留 source/run/raw/revision UUID、payload hash、row locator 與 contract／schema／parser／taxonomy／identity evidence／scope／formula／promotion policy 版本。Natural key 包含 instrument UUID、交易日、kind、scope、channel family／code。

LendingHistory 允許同一天不同 Channel，拒絕重複完整 natural key、Channel definition 漂移及不同來源／市場／kind／scope／unit／契約版本的任意混合。D+1 previous 是獨立 assertion，可能與 D 首版 balance 不同；不自動補資料、判定跨日必連續或覆寫舊版本。Revision 保存／Promotion 及交易邊界留待2E-2。

## 版本化來源契約

Manifest `lending-sources-1.0.0`，HTTP methods 全為 GET。所有 default_network_enabled=false；只有 metadata 設定與 Protocol，沒有任何可執行下載程式。Schema 欄位用 (group, source_name) 識別，重複名稱若在不同已知 group 是不同欄；同 group 重複／缺欄／未知欄拒絕。未知 grouped Schema 不依位置解析。

| Source／產品 | 實際 URL／參數 | 日期／unit／頻率 | 已核實能力與阻擋 |
| --- | --- | --- | --- |
| M3 TWSE 借券賣出 | `https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U`；date=YYYYMMDD、response=json | 回應西元date；股；daily | 只稽核2016-11-07，群組literal Schema／網站使用權待確認；2012-03-19前公式未核實，該分界不是全段Schema證明 |
| M4 TWSE 借券未還 | `https://www.twse.com.tw/rwd/zh/lending/TWT72U`；date=YYYYMMDD、selectType=SLBNLB、response=json | 西元date；股；daily；含上市／上櫃 | 只稽核2016-11-07 SLBNLB；Schema／使用權／市場label mapping待確認。SLB／NLB／LON／TWTBBU不當成已驗證子系列或partition |
| M6 TPEx 借券賣出 | `https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl`；無參數 | Date民國yyyMMdd；股；daily | Dataset11632 OGDL1／免費／每日，latest-only；既有body16欄契約保留，來源system channel未核實 |
| M7 TPEx dated 借券賣出 | `https://www.tpex.org.tw/www/zh-tw/margin/sbl`；date=YYYY/MM/DD、response=json | 回應西元date；股；daily | 只稽核2016-11-07；網站使用權、grouped literal Schema與2012格式分段待確認 |
| M8 TPEx 當日賣出 flow | `https://www.tpex.org.tw/openapi/v1/tpex_short_sell`；無參數 | ROC Date；成交張數，金額scale/currency未核實 | Flow-only，非balance；使用權映射／金額／instrument-period multiplier未確認，保持disabled |
| M9 TWSE 可借券賣出額度 | `https://openapi.twse.com.tw/v1/SBL/TWT96U`；無參數 | 無Date；Swagger稱股；預期daily | Quota-only；實際body、精確dataset11872映射、日期／刷新／effectivity未核實，保持disabled |

M3官方頁宣告2005-07-01起、M4宣告2004-11-22起、M7宣告2006-01起，均不是已取得歷史範圍。AUDITED_DATES_ONLY 要求明確日期且只允許已列的2016-11-07，但網站授權／Schema gate仍阻擋；latest-only 對任何指定日期都明確拒絕，不悄悄回最新。

M6 的 `SecuritiesBorrowingBalancePreviousDay／Sale／Return／Adjustment／BalanceOfTheMarketDay` 映射 LendingShortLedger，不能因英文字面 Borrowing 就當作借券未還。相鄰 `SaleBalancePreviousDay／SaleSell／SaleBuy／SaleSpotSecurities／SaleBalanceOfTheMarketDay／SaleAvailableVolumes` 明確標記 excluded_margin；本模組不計算融券公式。

M9 只有 `TWSECode／TWSEAvailableVolume／GRETAICode／GRETAIAvailableVolume` 的Swagger metadata，沒有帶日期的已驗證body；不能拿其他來源的日期或借券量填入。M4另有收盤價／估值元金額，契約保留其獨立語意，本次不計算金額或假定currency。

Source Contract 核對 product／report_kind、contract／schema、unit、channel、FormulaPolicy、官方日期與版本有效性。只有 verified Borrowing NONE／LendingShort SIGNED 政策可以宣告對應公式契約；Quota／Flow 不能附一個balance formula。Manifest另外保留使用權與body／Schema限制，不把「合法來源」等同「此階段已啟用」。

來源證據：

- [Phase 2A 已稽核端點與欄位](phase-2a-data-sources.md#5-c--融資融券與借券)。
- [TPEx Dataset11632](https://data.gov.tw/dataset/11632)：2026-11-10唯讀確認每日／免費／OGDL1，仍連legacy CSV並引用Swagger，不宣稱metadata直接等於相同body URL。
- [TPEx Swagger](https://www.tpex.org.tw/openapi/swagger.json)：本次回403，尊重限制，沒有重試繞過；M6沿用既有Phase2A／2D-2合法保存body的16欄契約，沒有新下載報表或宣稱本次重新驗證完整Swagger。
- [TWSE Swagger](https://openapi.twse.com.tw/v1/swagger.json)：本次僅核對M9 metadata，沒有下載未知body。
- [TWSE 借券資訊定義](https://www.twse.com.tw/zh/products/sbl/disclosures/info.html)、[政府資料開放授權條款](https://data.gov.tw/license)。

精確 OpenAPI 發布時間與 Rate Limit 未核實，Manifest 保留 null；20:30／22:30網站觀察／產製輪次不當作每筆source_published_at或SLA。Retry／timeout／rate limit 要由下一階段 Infrastructure 實作，本次沒有 I/O。

## 測試與驗證

三份新測試全明確標為 SYNTHETIC，只在記憶體建立固定資料，不當作正式行情或seed：

```bash
.tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_lending_models.py \
  apps/api/tests/test_lending_calculations.py \
  apps/api/tests/test_lending_source_contracts.py -q --tb=short

RUN_INTEGRATION=0 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest \
  apps/api/tests -m 'not integration and not network' -q --tb=short

PATH="$PWD/.tools/node/bin:$PATH" npm test
PATH="$PWD/.tools/node/bin:$PATH" npm run typecheck
PATH="$PWD/.tools/node/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 npm run build
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
```

覆蓋雙identity、正／負／零調整、完整／不完整分割、合計重複、Channel／來源／scope／日期／unit／版本不相容、缺欄與無效值、配額與flow隔離、發布與統計日期、payload證據、Decimal精度及Domain import邊界。

2026-11-10 實際結果：

| 檢查 | 結果 |
| --- | --- |
| Lending 固定模型／數值／來源契約 | **231 passed**：Models96、Calculator／Import邊界70、SourceContracts65 |
| 全後端不連網、非整合回歸 | **2,136 passed、426 deselected**，5.32秒；涵蓋既有Phase1／2B／2C／2D |
| Vitest | **244 tests、15 files passed** |
| TypeScript Typecheck | 通過 |
| Next.js Production Build | 通過；自動產生的 next-env.d.ts 已還原，沒有前端程式變更 |
| OpenAPI snapshot | `export_openapi.py --check` 通過，沒有新增或變動 API 契約 |
| Fresh Python process import Lending Domain／Ports | 通過，未載入FastAPI／SQLAlchemy／httpx／Redis／psycopg |
| `git diff --check`／新增檔案空白檢查 | 通過 |

426 deselected 是本次明確排除的 integration／network 測試，**不當成通過**。本次純Domain沒有SQL或I/O變更，不執行PostgreSQL寫入／Migration／真實資料匯入或容器重建。Build依既有執行環境於沙箱外完成，未部署；Pytest保留一項既有Starlette TestClient deprecation warning，沒有為此更換依賴。

## 2E-2 邊界與未解決事項

1. M3／M4／M7 網站授權、literal grouped Schema、歷史格式與有效日期仍需核實；M4市場映射必須連到既有 Instrument UUID。
2. M6僅latest-only；系統Channel／完整分割與精確更新時刻沒有核實，不宣稱十年歷史或可做官方分Channel合計。
3. M8金額／張股轉換／授權映射、M9日期／body／資源映射／限額生效日未解決。
4. 跨Raw Channel parts要先有可審查的source publication／snapshot binding，不能只比對timestamp或較晚下載時間。
5. 經後續任務授權後才實作 raw／audit／revision／canonical、Migration、來源 Adapter／Normalizer、持久 checkpoint、CLI／REST API與真實PostgreSQL／官方資料驗證。本次停止於Phase2E-1。
