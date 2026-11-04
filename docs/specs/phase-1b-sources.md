# Phase 1B — 官方資料來源查證

查證日期：2026-11-04，Asia/Taipei。本文件記錄官方規格、少量實際 HTTP 讀取結果與本階段能力邊界；筆數和日期是當次觀察，不是固定測試門檻或未來服務保證。

本文件描述原有 `stocks` 範圍；新增 `--scope securities` 的名錄分類、TWSE 全部行情匯出、幣別及獨立授權邊界見[全證券規格](taiwan-securities-ingestion.md)。原公司名錄含 TDR 等非普通股的限制，也由完整證券分類修正，不能將本文件中的公司名錄直接視為純普通股名單。

## 1. 採用的四個端點

| 市場／用途 | 官方 GET URL | 當次狀態及實際格式 | 當次資料量／日期 |
| --- | --- | --- | --- |
| TWSE 日行情 | [STOCK_DAY_ALL](https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL) | 200；UTF-8 中文欄位 CSV | 1,390 列；日期 1151103，即 2026-11-03 |
| TWSE 公司名錄 | [t187ap03_L](https://openapi.twse.com.tw/v1/opendata/t187ap03_L) | 200；中文欄位 JSON array | 1,099 家；出表日期 1151103 |
| TPEx 日行情 | [tpex_mainboard_daily_close_quotes](https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes) | 200；英文欄位 JSON array | 13,626 列；Date 1151104，即 2026-11-04 |
| TPEx 公司名錄 | [mopsfin_t187ap03_O](https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O) | 200；英文欄位 JSON array | 897 家；Date 1151103 |

官方規格與入口：

- [TWSE OpenAPI](https://openapi.twse.com.tw/)、[TWSE Swagger 2.0](https://openapi.twse.com.tw/v1/swagger.json)。規格列出上述兩個 TWSE 路徑，宣告 JSON 與 CSV 回應。
- [TPEx OpenAPI](https://www.tpex.org.tw/openapi/)、[TPEx OpenAPI 3.0 schema](https://www.tpex.org.tw/openapi/swagger.json)。server 是 `https://www.tpex.org.tw/openapi/v1`，上述兩個 GET 路徑均存在，宣告 JSON 與 CSV 回應。

TWSE 日行情即使送出 `Accept: application/json`，本次仍收到 CSV。因此 Adapter 必須檢查實際 body／格式，不可直接假設 `.json()` 一定成立。兩份 Swagger 的 response schema 描述單筆 object，實際 JSON body 是多筆 array；以實測 envelope 和正式欄位定義共同檢查，不能只照規格中的 object 判斷整份回應。

同一次查證中兩交易所的日行情日期不同，TPEx 公司名錄也比其行情日期早一天。同步必須保留各份行情的實際交易日及名錄的出表日，不能將它們全部覆寫為執行當天。

## 2. 日行情欄位對照

以下英文 TWSE 欄位來自官方 Swagger；中文欄位來自本次實際 CSV。TPEx 欄位同時經 Swagger 與本次 JSON 驗證。

| 意義／標準化欄位 | TWSE 中文 CSV | TWSE Swagger JSON | TPEx JSON |
| --- | --- | --- | --- |
| `trade_date` | 日期 | Date | Date |
| 來源股票代碼 | 證券代號 | Code | SecuritiesCompanyCode |
| 來源名稱 | 證券名稱 | Name | CompanyName |
| `open` | 開盤價 | OpeningPrice | Open |
| `high` | 最高價 | HighestPrice | High |
| `low` | 最低價 | LowestPrice | Low |
| `close` | 收盤價 | ClosingPrice | Close |
| `volume_shares` | 成交股數 | TradeVolume | TradingShares |
| `turnover` | 成交金額 | TradeValue | TransactionAmount |
| `trade_count` | 成交筆數 | Transaction | TransactionNumber |

`instrument_id` 由市場和官方公司代碼對應既有 Instrument，`source_id` 由採用的官方來源設定決定；不能使用來源顯示名稱作全域主鍵。

TWSE 還有 Change／漲跌價差。TPEx 還有 Change、Average、LatestBidPrice、`LatesAskPrice`（官方原始拼字）、Capitals、NextReferencePrice、NextLimitUp、NextLimitDown；這些欄位保留在 raw response，不能拿均價、參考價、買價或賣價代替缺失 OHLC。

官方 [TWSE 行情資料集](https://data.gov.tw/dataset/11549) 與 [TPEx 行情資料集](https://data.gov.tw/dataset/11370) 的欄位說明，以及兩份 Swagger，均把對應 volume 欄位定義為股數。因此上述來源直接使用股，不乘 1,000。這個判定只適用於已查證的端點；其他以張揭示的來源需要另外確認。

## 3. 公司名錄與股票範圍

| 用途 | TWSE 公司名錄 | TPEx 公司名錄 |
| --- | --- | --- |
| 公司代碼 | 公司代號 | SecuritiesCompanyCode |
| 完整名稱 | 公司名稱 | CompanyName |
| 顯示簡稱 | 公司簡稱 | CompanyAbbreviation |
| 出表日期 | 出表日期 | Date |
| 掛牌日期 | 上市日期 | DateOfListing |

TPEx 的 `Symbol` 是公司英文簡稱，**不是股票代碼**。TPEx 的 DateOfListing 本次為西元 `YYYYMMDD`；行情 Date 則為民國 `yyyMMdd`。例如行情 1151104 代表 2026-11-04；名錄掛牌日期 20180808 代表 2018-08-08，不可對兩者都加 1911。

行情端點包含公司股票以外的證券，本次 TPEx 的 13,626 列含大量權證／ETF 等。Phase 1B 以官方公司名錄建立股票 universe，不以「代碼看起來像股票」或所有行情列均為 stock 作假設。名錄未涵蓋的行情列應有明確排除統計／紀錄，不自動建立成普通股票。

本次 TWSE 有 1,098 列行情匹配公司名錄；TPEx 有 893 列匹配。TPEx 名錄中的 4207、4804、5355、8176 未出現在當次行情，僅此現象不足以判定下載截斷，也不能替它們生成零成交價格或 bars。新掛牌公司若尚未被名錄涵蓋，必須保留未對應原因並等待名錄更新或經核實的 mapping；不得憑代碼臆造分類。

名錄及行情的最新快照不能證明歷史某日的上市／上櫃成員範圍。本階段不宣稱已處理完整的歷史退市名錄、代碼重用或歷史身分變更。

## 4. 特殊值與不完整資料觀察

來源數值以文字呈現，解析須先處理首尾空白、逗號、明確缺值標記，再以 Decimal／整數驗證。不能將零、空字串、NULL、缺欄位、破折號全部當成相同值。

本次 TPEx 快照有 7,579 列至少一個 OHLC 是缺值標記，7,571 列成交股數為 0；限於公司名錄匹配的股票，分別為 12 列與 5 列。這些是資料觀察，不是允許靜默略過異常的比例門檻。

- 020001：OHLC 為 `---`／帶前置空白的 ` ---`，成交股數、金額、筆數均為文字 0。
- 2235：同樣缺 OHLC，但成交股數為 212、金額 6486、筆數 8。因此缺 OHLC 不等於所有類型交易都沒有成交。

兩種情境都不可補造 canonical 的正數 OHLC，也不可用 0 違反 Phase 1A 價格約束。應保存 raw、來源列識別、處理結果與具體原因；對個別缺價／異常列隔離處理，不因一檔異常默默丟掉其餘有效資料。除非來源另外提供明確狀態，本階段不由缺價自動斷言「停牌」。

## 5. 更新頻率、授權與來源顯名

| 官方資料集 | 查證時的 metadata 更新頻率 | 授權／計費 |
| --- | --- | --- |
| [TWSE 個股日成交資訊，11549](https://data.gov.tw/dataset/11549) | 每日 | OGDL 第 1 版；免費 |
| [TWSE 上市公司基本資料，18419](https://data.gov.tw/dataset/18419) | 每月 | OGDL 第 1 版；免費 |
| [TPEx 上櫃股票行情，11370](https://data.gov.tw/dataset/11370) | 每日 | OGDL 第 1 版；免費 |
| [TPEx 上櫃公司基本資料，25036](https://data.gov.tw/dataset/25036) | 每日 | OGDL 第 1 版；免費 |

上述是政府資料集 metadata 所列頻率，不能據此推定各 API 的精確產製時刻、發布延遲或可用性保證。特別是 TWSE 公司資料集目前列為每月，不能改寫為官方保證每日刷新；仍須保存實際回應的出表日期。

[政府資料開放授權條款第 1 版](https://data.gov.tw/license) 允許在條款範圍內重製、整理及再利用，使用與衍生結果須清楚顯示原始提供機關與授權來源。請保留資料提供機關、TWSE／TPEx、資料集名稱、取得時間、原始端點及授權連結，並指出標準化或篩選屬本平台處理，避免暗示交易所背書。

[TWSE 網站使用條款](https://www.twse.com.tw/zh/terms/use.html) 與 [TPEx 網站使用條款](https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html?l=zh-tw) 對一般網站下載及內容再利用另有規範，同時說明政府開放資料的例外與來源標示義務。本次採用正式 OpenAPI／已列入政府開放資料的資源，不將 OpenAPI 授權延伸宣稱為整個網站或付費歷史商品的任意抓取權。

建議顯名方式：原始資料提供機關／單位與資料集名稱、資料年度、TWSE 或 TPEx 官方來源網址、OGDL 1.0 連結；另標示本平台已做格式標準化、股票 universe 篩選與品質檢查。

## 6. 限流、逾時與重試能力邊界

在本次讀取的 Swagger、官方資料集與使用條款中，未找到可確認的數字 QPS／每日 quota、精確發布時間或 SLA。這些項目明確列為未確認，不引用第三方網站的數字充作官方要求。

專案採用預設至少 5 秒請求間距、最多 3 次嘗試，配合明確 timeout 與有限退避。這些數值是降低服務負擔的工程預設，**不是官方公布的限流上限**。多進程、同一 IP 或其他使用者的請求可能疊加，不能因單一進程有等待就宣稱符合尚未查得的官方 quota。

對 429／可重試的暫時性失敗，應尊重 Retry-After 並有限重試；認證／權限錯誤、schema 變更或資料驗證錯誤不能靠無限重試解決。失敗回應及錯誤摘要應可追溯，外部失敗不應先刪除既有 canonical bars。

## 7. 歷史資料與手動回補限制

TWSE `STOCK_DAY_ALL` 與 TPEx `tpex_mainboard_daily_close_quotes` 的已查證 GET 規格都沒有日期查詢參數；此次各回傳一個交易日。不能把相同 body 換上指定日期，或自行加未確認參數後宣稱取得歷史行情。

Phase 1B 的手動 backfill 僅重播**已保存的官方原始 response**，並檢查其內含交易日符合指定日期。沒有相符 archive 時應清楚回報無可用資料，不由最新快照補造歷史；archive 的可回補範圍取決於實際保存內容。

雖然官方網站另有歷史查詢頁，本次未確認任意歷史日期的正式 API 契約與完整授權能力，因此**沒有完成也不宣稱完整遠端歷史回補**。未來若新增已核實的歷史資料來源，需另行查證 schema、日期參數、單位、授權與限流，並新增固定 fixture 及網路測試。
