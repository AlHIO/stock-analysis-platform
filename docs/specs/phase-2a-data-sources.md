# Phase 2A — 官方籌碼資料來源查證

查證日期：**2026-11-07，Asia/Taipei**。本文件是資料來源稽核，不是已完成的 importer。僅讀官方頁面、官方 schema，以及少量有 timeout／大小界線的公開樣本；未匯入正式資料、未修改資料庫或正在執行的行情 worker、未購買訂閱、未繞過驗證碼或存取限制。

搭配：[Domain／ERD／匯入流程](phase-2-domain-model.md)、[API 草案](phase-2-api-contract.md)、[2B–2F 工作與驗收](phase-2-roadmap.md)。以下筆數與日期都是當次觀察，不是固定測試值、服務 SLA 或完整歷史證明。

## 1. 查證標記與決策

- **HTTP 已驗證**：真正取得回應並檢查 envelope／欄位；若只讀前綴會另外註明，不能稱全檔驗證。
- **文件已驗證**：官方頁／Swagger／資料集 metadata 有此說明；不表示每個歷史日期都已下載。
- **語意待核實**：有資料但日期、單位、分類或修訂規則仍不足，不允許猜測 canonical。
- **授權待核實**：公開可讀不等於已取得自動擷取／轉載權；未核實前不得開啟該路徑的正式自動匯入或公開資料服務。
- **Deferred**：暫無合適且已核可的自動來源，或需另行訂閱／歷史契約；不使用非官方站點代替。

| ID    | 官方資料／市場                   | 當次結論                                            | 建議                                                 |
| ----- | -------------------------------- | --------------------------------------------------- | ---------------------------------------------------- |
| I1    | TWSE 三大法人買賣超日報 T86      | 2016／2026 指定日期成功；16→19欄；現行完整法人分類  | Domain可先設計；網站自動利用／轉載權及歷史分類待核實 |
| I2    | TPEx 三大法人 OpenAPI            | 最新日期明確；實際20欄，缺完整自行買賣／避險        | 可作有限資訊／對照，不能宣稱滿足完整法人需求         |
| I3    | TPEx 三大法人日期匯出            | 2016／2026成功；16→24欄，完整現行分類               | 完整明細候選；歷史語意／網站使用權 gate              |
| O1    | TDCC 股權分散 CSV                | 全檔69,955列、統計日2026-11-06；資料集OGDL1.0       | ownership第一個正式候選，從觀察到的週快照累積        |
| O2    | TDCC 股權分散 JSON               | HTTP200，只檢查有界前綴；日期2026-10-30             | 獨立來源表示法，不自動覆蓋較新CSV                    |
| O3    | TDCC 股權分散 HTML歷史           | 正常表單查得2330／2025-11-07；當前52個週日期        | 最近一年技術可查；批次利用契約待核實，非十年API      |
| M1    | TWSE MI_MARGN OpenAPI            | HTTP200但無交易日期欄位                             | 日期未可信確認前不能寫入日 canonical                 |
| M2–M4 | TWSE 融資融券／借券歷史匯出      | 指定2016-11-07成功；不同表與不同單位                | 有技術能力，網站自動利用／轉載權 gate                |
| M5–M6 | TPEx 融資融券／融券借券 OpenAPI  | 最新日成功，分別有張／股單位；OGDL資料集佐證        | margin初版優先候選，先核實換算與語意                 |
| M7    | TPEx 融券借券日期匯出            | 2016-11-07成功                                      | 歷史格式／網站使用權 gate                            |
| M8    | TPEx 當日融券／借券賣出量值      | 最新日成功，volume為張                              | 金額單位／個別資料集權限仍待核實，補充候選           |
| M9    | TWSE／TPEx 可借券賣出額度 TWT96U | 只驗證Swagger列路徑與四欄，未讀實際body；無宣告日期 | metadata-only，額度不是實際成交或餘額                |
| B1    | TWSE 公開券商分點查詢            | CAPTCHA／安全限制；未取得報表                       | 自動化 Deferred                                      |
| B2–B3 | TWSE／TPEx 官方付費分點商品      | 商品與歷史範圍存在；未取得遞送契約                  | Deferred，需訂閱與使用範圍核可                       |

## 2. 共通使用限制、限流與發布時間

三家來源本次均**未找到可依賴的數值 QPS／每日配額或 API SLA**。不得寫成「不限流」。專案既有每host至少5秒、最多3次、有限timeout／response-size是工程政策，不是官方額度或授權證明。若多個worker共用host/IP，需協調總流量；目前 limiter 僅在同process/event loop共享。429尊重Retry-After，403／登入／CAPTCHA立即停止；不切換IP、代理或替代網域規避。

[TWSE 使用條款](https://www.twse.com.tw/zh/terms/use.html)第6點對非經同意方式的自動下載有限制，第8點區分另行授權政府資料開放平台的資料。故 OpenAPI／特定OGDL資料集、一般網站日期匯出、付費商品是**不同授權範圍**；有來源註記也不等於取得所有散布權。TWSE [Swagger](https://openapi.twse.com.tw/v1/swagger.json) 的授權連結僅作其已列路徑的證據，T86／TWT93U／TWT72U不能假裝是其中已發布的OpenAPI。

TPEx [OpenAPI schema](https://www.tpex.org.tw/openapi/swagger.json)、各政府資料集與[網站條款](https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html?l=zh-tw)分別保留證據。該條款頁當次直接存取403，未繞過；不能聲稱已逐條取得現行全文。TDCC O1的[資料集11452](https://data.gov.tw/dataset/11452)明列[OGDL1.0](https://data.gov.tw/license)，不自動擴張到整個HTML歷史查詢服務。OGDL範圍內保留來源與授權標示，不暗示交易所／集保背書。

除下列明示的近似發布輪次外，**精確公開API更新時刻均未核實**。交易日／統計日、來源發布日／時刻、HTTP取得時間與入庫時間必須分開。付費商品的產製時刻不能套到公開API；HTTP Date、Last-Modified、下載檔名也不是實際發布時間的保證。

這項條款查證也提示既有Phase1網站匯出的適用權限仍應另行確認；Phase2A只記錄風險與待確認項，沒有更動既有行情資料、設定或工作。

## 3. A — 三大法人買賣超

### I1 TWSE 三大法人買賣超日報

官方頁：[T86](https://www.twse.com.tw/zh/trading/foreign/t86.html)，其HTML明示 `data-api=/fund/T86`。

- 實際 endpoint：`GET https://www.twse.com.tw/fund/T86`。
- Parameters：`date=YYYYMMDD`（西元交易日）、`selectType=ALL|ALLBUT0999`、`response=json`。樣本使用ALLBUT0999；此選項排除權證等，**仍非僅普通股**。JSON已實測；頁面另提供CSV／HTML。
- Response：`stat/date/title/hints/fields/data/selectType/notes/total` JSON；date為西元YYYYMMDD。數字為可帶逗號的字串，數量欄明示股；名稱有尾端空白。不可將展示張數概念再乘1,000。
- 頻率：每日。公開endpoint確切發布時刻未核實。
- 歷史：官方頁宣告自2012-05-02；實測2016-11-07 HTTP200/statOK、853列16欄；2026-11-06 HTTP200/statOK、1,354列19欄。兩日成功不證明中間無缺日或自2012起都同schema。
- 範圍：回應說明含一般、零股、盤後定價與鉅額，排除拍賣／標購；原始成交統計不等於更帳修正後序列。保存來源選項及交易範圍，不混同付費不含鉅額商品。
- 權限：未確認T86本身的自動利用／公開轉載契約；在當次TWSE OpenAPI路徑未列出T86。政府網站「建議開放資料」頁不是授權資料集。

現行19欄完整對照：

| Canonical類別      | 原始欄名（單位：股）                                                                             | 加總角色                       |
| ------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------ |
| identity           | 證券代號、證券名稱                                                                               | 經市場／日期核實Instrument     |
| foreign_ex_dealers | 外陸資買進股數(不含外資自營商)、外陸資賣出股數(不含外資自營商)、外陸資買賣超股數(不含外資自營商) | leaf                           |
| foreign_dealers    | 外資自營商買進股數、外資自營商賣出股數、外資自營商買賣超股數                                     | supplemental，不再加進三大總計 |
| investment_trust   | 投信買進股數、投信賣出股數、投信買賣超股數                                                       | leaf                           |
| dealer_total       | 自營商買賣超股數                                                                                 | subtotal，買／賣未提供則null   |
| dealer_proprietary | 自營商買進股數(自行買賣)、自營商賣出股數(自行買賣)、自營商買賣超股數(自行買賣)                   | leaf                           |
| dealer_hedging     | 自營商買進股數(避險)、自營商賣出股數(避險)、自營商買賣超股數(避險)                               | leaf                           |
| three_major_total  | 三大法人買賣超股數                                                                               | subtotal，不再疊加子項         |

2016的16欄只有「外資買進／賣出／買賣超股數」，沒有現代外資自營商拆分／排除註記。不得只因算式相符就把舊外資欄套成現代分類；須另外核實歷史分類生效界線，缺項不是0。

官方授權替代候選：[三大法人買賣超付費商品](https://eshop.twse.com.tw/zh/product/detail/c4c87ac184e44896a05fcab5a9d544ec) metadata列2004-09-09起，TWT86UC不含鉅額18:00、TWTAIUC含鉅額20:00、每日TEXT／Excel；內用／外用契約不同。商品metadata GET可讀，但登入後遞送URL／認證參數／訂閱權均未驗證；這不是免費T86歷史能力的證明。

### I2 TPEx 三大法人 OpenAPI（最新快照）

- `GET https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading`，官方schema無日期參數。HTTP200 JSON array，923列，`Date=1151106`（民國yyyMMdd→2026-11-06）。每日，精確刷新時刻未知；**只核實latest，無任意日期能力**。
- [資料集11856](https://data.gov.tw/dataset/11856)明列每日、免費、OGDL1.0並引用Swagger；實際CSV連結仍是legacy `https://www.tpex.org.tw/web/stock/3insti/DAILY_TradE/3itrade_hedge_result.php?l=zh-tw&o=data&se=EW&t=D`，本次403，未繞過。記錄legacy資源與現行OpenAPI映射，不能擴張為所有AL歷史匯出的授權。
- 實際JSON及Swagger同為20欄，**不能提供完整自行買賣／避險三元組**。其完整keys如下；保留官方空白與拼字，alias須有碰撞檢查：

```text
Date; SecuritiesCompanyCode; CompanyName
Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy
 Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell
Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference
Foreign Dealers-Total Buy; Foreign Dealers-TotalSell; ForeignDealers-Difference
ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy
ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell
ForeignInvestorsInclude MainlandAreaInvestors-Difference
SecuritiesInvestmentTrustCompanies-TotalBuy
SecuritiesInvestmentTrustCompanies-TotalSell
SecuritiesInvestmentTrustCompanies-Difference
Dealers-TotalBuy; Dealers-TotalSell; Dealers-Difference
Dealers -TotalSell; TotalDifference
```

`Dealers -TotalSell`的官方description與完整日期匯出核對是避險賣出，不是另一個總賣出；不能用這一欄加total反推所有缺少的細項。數量為股，Date為民國；foreign inclusive是排除外資自營商＋外資自營商的重疊subtotal，不能再加一次。923列與AL匯出的5,062列是當次範圍差異，不足以單憑筆數斷定截斷或完整股票名單。

### I3 TPEx 法人日期匯出

官方頁：[三大法人明細日報](https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html)。官方JS action=`insti/dailyTrade`，瀏覽器表單使用POST；**本次真正驗證的資料匯出方法為GET**，不宣稱POST已測。

- Endpoint：`GET https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade`。
- Parameters：`type=Daily`、`sect=AL`、`date=YYYY/MM/DD`（西元）、`response=json`。頁面另有EW排除權證等選項，非普通股專屬。HTML匯出也經核對多層欄頭。
- Response：JSON `columnNum/tables/template/date/stat`；topdate西元YYYYMMDD，tabledate可為民國115/11/06；數量為股。每日，精確公開發布時刻未知。
- 2016-11-07 HTTP200/statok，2,204列16欄；2026-11-06 HTTP200/statok，5,062列24欄。有效表與空表的位置會交換，不能固定tables[0]；columnNum曾標25而fields與rows實為24，應用完整schema核對。
- 現行24欄：0/1代號名稱；2–4外資不含外資自營商、5–7外資自營商、8–10外資含自營商subtotal、11–13投信、14–16自行買賣、17–19避險、20–22自營商subtotal，各組為買進股數／賣出股數／買賣超股數；23三大法人總淨額。重複平面欄名不得直接zip成dict；需已驗證多層group與版本。
- 當次5,062列皆通過各組buy−sell=net、外資inclusive、dealer=self+hedge及三大總計核對。總計只加foreign_ex+trust+dealer_total；不再加foreign_dealers或dealer子項。
- 2016舊16欄缺外資自營商拆分，需單獨歷史契約。來源含一般、鉅額、零股與投信綜合交易帳戶量。
- 官方頁主入口宣告2007-04-20起，另連2004-06..2006-12、2007-01-02..04-20、2007-04-21..2014-11-30的舊格式。不能把最早日期當現行schema起點，也不能宣稱所有日期連續可取。
- 狀態：技術與兩個時期schema已驗證；歷史完整性／分類界線／自動利用與全AL轉載權仍待核實。

## 4. B — TDCC 集保戶股權分散

官方證據：[查詢頁](https://www.tdcc.com.tw/portal/zh/smWeb/qryStock)、[資料集11452](https://data.gov.tw/dataset/11452)、[OpenAPI](https://openapi.tdcc.com.tw/)、[機器schema](https://openapi.tdcc.com.tw/tdcc-opendata-api-docs)。統計基礎為每週最後營業日結束後的集保登載餘額、經ID歸戶；某些專戶按專戶股數計算。不能推成可識別的個人交易紀錄。

| ID  | Endpoint／Method／Parameters                                                     | Response及日期                                                                                                                                                             | 歷史／發布／授權                                                                                                                          |
| --- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| O1  | `GET https://opendata.tdcc.com.tw/getOD.ashx?id=1-5`，id=1-5，無核實日期selector | HTTP200 UTF-8 CSV；全檔2,395,996 bytes、69,955列，全部資料日期20261106                                                                                                     | latest-only；週統計，精確發布時刻未知；11452明列OGDL1.0                                                                                   |
| O2  | `GET https://openapi.tdcc.com.tw/v1/opendata/1-5`，schema無參數                  | HTTP200 JSON array；僅讀524,288-byte前綴，advertised長度9,871,491；前綴日期20261030                                                                                        | latest-only；當次比CSV舊一週，不能稱全檔均此日期；11452引用此API說明，仍獨立保留表示法與日期                                              |
| O3  | `GET`及正常session `POST https://www.tdcc.com.tw/portal/zh/smWeb/qryStock`       | HTML；POST scaDate=20251107、sqlMethod=StockNo、stockNo=2330、stockName空、method=submit、firDate為頁面最新日；沿用正常GET產生的SYNCHRONIZER_TOKEN／URI，不存token值於repo | HTTP200結果明示114年11月07日／2330／17列；當前selector52日期2025-11-07..2026-11-06；未見CAPTCHA，若未來出現即停止；批次利用／轉載權待核實 |

官方說明「自2008-07建置，保存期間一年」：建置起點不是公開可取得完整十年歷史的保證。O1/O2沒有已核實的任意歷史參數；O3正常單次查詢成功不等於合法穩定的批次API。較早資料需另查官方授權交付來源，**Deferred，未採用第三方備份**。

| Canonical        | 原始欄位          | 格式／單位                                                                      |
| ---------------- | ----------------- | ------------------------------------------------------------------------------- |
| as_of_date       | 資料日期          | CSV/JSON西元YYYYMMDD；HTML可為民國文字；保留實際統計日，不硬編碼週五            |
| source_symbol    | 證券代號          | 字串，保留前導0；CSV大量尾端空白，2330實為`2330  `；無市場欄                    |
| bucket_code      | 持股分級          | 1..17，依有版本級距表解讀                                                       |
| holder_count     | 人數              | 人數／來源定義歸戶計數；一般級距非負，adjustment可能空或非0，不能再加進人數合計 |
| quantity         | 股數              | 股／受益單位，依Instrument核實；不乘1,000                                       |
| reported_percent | 占集保庫存數比例% | Decimal百分比、分母集保庫存；不是全部發行股本或0..1 fraction                    |

級距（含邊界）：1=1–999、2=1,000–5,000、3=5,001–10,000、4=10,001–15,000、5=15,001–20,000、6=20,001–30,000、7=30,001–40,000、8=40,001–50,000、9=50,001–100,000、10=100,001–200,000、11=200,001–400,000、12=400,001–600,000、13=600,001–800,000、14=800,001–1,000,000、15=1,000,001以上；16=差異數調整、17=合計。官方[資料標準](https://schema.nat.gov.tw/lists/75)與[級距附件](https://schema.nat.gov.tw/gsp/frontstage/resource.download/9f7d9184-20a0-49eb-b7dd-96e23686c4ef)有索引證據，但附件當次直接取得502，未稱下載成功；歷史查詢HTML另獨立驗證了同套級距。

重要品質問題：

1. JSON實際日期key為 `\ufeff資料日期`，schema寫乾淨key；只允許明確BOM alias並檢查碰撞。CSV有63,852列代碼尾端空白；保留raw後才trim，不以長度／外觀推斷市場或股票分類。
2. 2026-11-06 CSV2330：regular股數總和25,932,390,067，adjustment=+20,000、holders=3，total=25,932,370,067；此例total=detail−adjustment，adjustment人數不參與total。當次CSV全部4,115組皆符合detail−adjustment、total人數=sum regular人數，其中99組adjustment非0；只證明此份表示法，不外推所有歷史檔。2025-11-07 HTML2330：adjustment=−1,999、holder空，total=detail+signed adjustment。**不能跨表示法套一個固定正負號CHECK**。
3. 同份CSV2330一般級距比例和99.94、來源合計100.00，不能重縮放成100或當作浮點誤差丟棄；精度／截位規則需核實。
4. 最高級距從1,000,001開始，無法精確求「≥1,000,000股」；落在級距中間的門檻不得比例拆估。
5. 11452 metadata寫每1月，來源週統計說明與實際日期列表為每週；保留metadata矛盾，模型仍用實際週統計，不變成月資料。
6. 特殊專戶與待領回零股／支付憑單影響統計；分布改變可能來自級距跨越、股務事件或保管移轉，不能識別個人買賣。

O1當次SHA256=`5332a48c7b9c68f01c4912f57fe767fb23fb9a8718d0b92b3ae1bbeda5cd18c9`。這是稽核指紋，不是未來同步必須相同的值。

## 5. C — 融資融券與借券

所有下列已實測資料方法為GET。OpenAPI未宣告日期參數者只核實最新快照；帶日期網站匯出為另一來源契約。數值一律先當來源字串處理，避免float。

| ID  | 官方來源／實際URL與parameters                                                                                        | Response／當次日期與資料                                                 | 歷史能力／來源狀態                                                                                                                                    |
| --- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| M1  | `https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN`，無參數                                                     | HTTP200 JSON array1,306列；**沒有日期欄位**，空字串與數值並存            | latest-only；已列Swagger並有OGDL連結，日canonical仍被缺可信日期阻擋                                                                                   |
| M2  | `https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20161107&selectType=ALL&response=json`；date西元YYYYMMDD | HTTP200/statOK/date20161107；tables，945個證券列＋3列市場summary         | [官方頁](https://www.twse.com.tw/zh/trading/margin/mi-margn.html)宣告2001-01-01起；單日成功非連續覆蓋證明；網站自動利用／轉載權待核實                 |
| M3  | `https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?date=20161107&response=json`                                    | HTTP200/statOK/date20161107；fields/groups/data、948列，hints明示單位股  | [官方頁](https://www.twse.com.tw/zh/trading/margin/twt93u.html)宣告2005-07-01起，2012-03-19前後公式有變；網站權限待核實                               |
| M4  | `https://www.twse.com.tw/rwd/zh/lending/TWT72U?date=20161107&selectType=SLBNLB&response=json`                        | HTTP200/statOK/date20161107；1,121列，含集中／櫃檯市場別，股數與元金額   | [官方頁](https://www.twse.com.tw/zh/products/sbl/disclosures/twt72u.html)宣告2004-11-22起；SLBNLB為合計，SLB/NLB/LON/TWTBBU為不同選項；網站權限待核實 |
| M5  | `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance`，無參數                                           | HTTP200 JSON array919列，Date1151106；數量為張、使用率為%                | latest-only；[11387](https://data.gov.tw/dataset/11387)每日／免費／OGDL1.0並引用Swagger；融資歷史機器endpoint未核實                                   |
| M6  | `https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl`，無參數                                                         | HTTP200 JSON array932列，Date1151106；數量為股                           | latest-only；[11632](https://data.gov.tw/dataset/11632)每日／免費／OGDL1.0並引用Swagger；不可把英文字面借券誤當借券總餘額                             |
| M7  | `https://www.tpex.org.tw/www/zh-tw/margin/sbl?date=2016%2F11%2F07&response=json`；date西元YYYY/MM/DD                 | HTTP200/statok/date20161107，tables、626列；單位股                       | [官方頁](https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/sbl.html)2006-01起，另有2012格式分段；網站權限待核實                          |
| M8  | `https://www.tpex.org.tw/openapi/v1/tpex_short_sell`，無參數                                                         | HTTP200 JSON array1,010列、Date1151106；當日融券／借券賣出成交張數及金額 | latest-only；金額精確單位、對應資料集使用權需再核實，不能當作餘額來源                                                                                 |

M2/M3/M4由官方HTML `data-api`與選項取得；M7由官方JS action=`margin/sbl`取得。不是猜測OpenAPI路徑；目前TWSE Swagger沒有TWT93U／TWT72U。另有Swagger已列 `GET https://openapi.twse.com.tw/v1/SBL/TWT96U`，本次不把它當成已完成body／歷史驗證的來源；見下方待確認項。

M9的官方Swagger沒有request parameters，宣告JSON／CSV、四個字串欄：`TWSECode`（上市證券代號）、`TWSEAvailableVolume`（上市可借券賣出股數）、`GRETAICode`（上櫃證券代號）、`GRETAIAvailableVolume`（上櫃可借券賣出股數），未宣告Date。schema宣告單筆object而非已實測envelope；不推定真實回應結構。單位文件為股、預期日額度，但實際資料／日期／刷新時間未驗證；[資料集11872](https://data.gov.tw/dataset/11872)有每日／OGDL線索，與此body的精確資源映射仍待確認。此來源不符合借券實際成交或餘額需求，暫不採用；沒有用其他來源的日期替它補日期。

### 欄位、單位與發布規則

| 來源  | 已核對的欄位                                                                                                                                                                                                                                                                                                                                                                                                                                            | 單位／日期／發布注意                                                                                                                            |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| M1/M2 | 股票代號／名稱；融資與融券各自買進、賣出、現金或現券償還、前日餘額、今日餘額、限額；資券互抵、註記                                                                                                                                                                                                                                                                                                                                                      | 交易單位／張；M2市場summary另有融資金額(仟元)，不可混成每股資料。selectType=MS是市場彙總而非證券列。M1沒有交易日，禁止補下載日                  |
| M3/M7 | 代號名稱；融券前日、賣出、買進、現券償還、今日、限額；借券賣出前日、賣出、還券、調整、今日、次日限額；註記                                                                                                                                                                                                                                                                                                                                              | 股；同名「前日餘額」分兩個group。約20:30與22:30兩輪，實際完成依回報／結算，不是SLA；可有次日更帳與市場移轉                                      |
| M4    | 市場別；前日借券餘額(1)股、本日異動股借券(2)、本日異動股還券(3)、本日借券餘額股(4)=(1)+(2)-(3)、本日收盤價(5)單位：元、借券餘額市值單位：元(6)=(4)\*(5)                                                                                                                                                                                                                                                                                                 | 股、估值價格及市值元；借入未還不等於已賣出。公開發布時刻未知；合計與子系統不可重複相加                                                          |
| M5    | Date/SecuritiesCompanyCode/CompanyName；MarginPurchaseBalancePreviousDay/MarginPurchase/MarginSales/CashRedemption/MarginPurchaseBalance/MarginPurchaseBalanceBelongSecuritiesFinanceEnterprise/MarginPurchaseUtilizationRate/MarginPurchaseQuota；ShortSaleBalancePreviousDay/ShortSale/**ShortConvering**/StockRedemption/ShortSaleBalance/ShortSaleBalanceBelongSecuritiesFinanceEnterprise/ShortSaleUtilizationRate/ShortSaleQuota；Offsetting/Note | Date民國yyyMMdd；數量張，使用率Decimal百分比；官方拼錯的ShortConvering是買進還券。Note可能是次一營業日狀態，不是發布日；精確OpenAPI刷新時刻未知 |
| M6    | Date/SecuritiesCompanyCode/CompanyName；SaleBalancePreviousDay/SaleSell/SaleBuy/SaleSpotSecurities/SaleBalanceOfTheMarketDay/SaleAvailableVolumes；SecuritiesBorrowingBalancePreviousDay/SecuritiesBorrowingSale/SecuritiesBorrowingReturn/SecuritiesBorrowingAdjustment/SecuritiesBorrowingBalanceOfTheMarketDay/AvailableVolumesForSBLShortSale；Note                                                                                                 | 股；後組中文語意為借券**賣出**，非借券未還總額；日期民國。頁面20:30/22:30輪次不等於OpenAPI一定同時更新                                          |
| M8    | Date/SecuritiesCompanyCode/CompanyName/ShortSaleVolume/ShortSaleAmount/SBLVolume/SBLAmount                                                                                                                                                                                                                                                                                                                                                              | Swagger明示volume成交張數；金額scale/currency待核實；是當日flow，不是balance                                                                    |

M5/M6同日00411A的融券餘額分別103與103000，與張／股差異一致；這只是換算佐證，不能由一檔樣本推定所有ETF或外國證券每張皆1,000。須依官方[交易單位規則](https://accessibility.twse.com.tw/zh/products/system/trading.html)與Instrument適用期間核實 conversion factor。

已知品質／公式：

- 融資今日＝前日＋買進−賣出−現金償還；融券今日＝前日＋賣出−買進−現券償還；借券賣出今日＝前日＋賣出−還券＋signed調整；借券未還今日＝前日＋借券−還券。依來源已核實的調整口徑驗證，不互換兩種ledger。
- [信用統計說明](https://www.twse.com.tw/exchangeReport/MI_MARGN?response=html&selectType=MS)指出晚間處理後公布、可能次日更帳；D+1報表的前日餘額可能與D最初公布不同，不能直接覆寫D且丟失原始lineage。
- [借券資訊說明](https://www.twse.com.tw/zh/products/sbl/disclosures/info.html)區分借券未還與借券賣出。M3/M7特定避險情況賣出可高於一般限額，**不能一律CHECK sold≤limit**。
- 歷史回應notes可能仍含現行規則，例如2016樣本有2020生效說明；notes不是point-in-time法規或當日有效規則的證明。
- M4包含兩市場，必須依市場別dispatch；不為TPEx證券再重複建立TWSE Instrument。市場轉換、已下市代碼仍沿用歷史身分防護。
- TPEx融資歷史[查詢頁](https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html)當次403；索引顯示2007起及2003–2006舊格式，但**未核實實際dated machine endpoint**，不能拿M7借券表冒充融資歷史。

M5/M6政府資料集仍連legacy CSV，並引用現行Swagger；保留此映射證據，不說metadata直接連到現行body。個別金額單位與自動利用未確認的來源保持disabled。付費[TWT72U](https://eshop.twse.com.tw/zh/product/detail/8247357531294f4ab139f64c96394096)商品稱2017-08-07起、20:30；[TWT93U](https://eshop.twse.com.tw/zh/product/detail/000000006e0bbe8d016f181acc1f0321)稱2019-12-23起、23:30。這些是商品範圍／產製時間，不能替代公開匯出的起點或刷新保證；遞送API與訂閱均未取得。

## 6. D — 券商分點：Deferred

| 來源            | 實際查證的URL／Method                                                                                             | 能力與限制                                                                                                                                                                                                     | 決策                                               |
| --------------- | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| B1 TWSE公開分點 | `GET https://bsr.twse.com.tw/bshtm/bsWelcome.aspx`（入口`https://bsr.twse.com.tw/bshtm/`）                        | 官方索引說明按證券查詢需CAPTCHA、僅當日；直接取得安全阻擋文字，未送查詢。資料endpoint/request schema未驗證，不提供繞過流程。索引列一般/零股15–16時、鉅額17–18時，非已實測SLA                                   | 自動擷取Deferred                                   |
| B2 TWSE授權商品 | `GET https://eshop.twse.com.tw/zh/product/detail/34e615c34c4e4ca3b23fc732bd736f94`（商品metadata，非資料遞送API） | 券商每日買賣證券明細，2012-10-25起；每日ZIP，regular16:10／block17:30；code/broker/price元/buyshares/sellshares；內用與外用契約不同；[分類頁](https://eshop.twse.com.tw/zh/category/sub/29)另有含權證產品      | 訂閱、預算、對外服務及實際遞送契約未核可，Deferred |
| B3 TPEx授權商品 | `GET https://eshop.tpex.org.tw/zh/product/detail/E982FB3B7484B8AD66AF83E6F31F0CDB`（metadata）                    | 2014-12-01起；每日STKRPQSTK.EXE regular16:00、STKGMHSTK.CSV block17:15；日期西元、券商代碼／名稱、價元、買賣股數；[權證商品](https://eshop.tpex.org.tw/zh/product/detail/23F0FF130FA5C09FB1FD7046AFFA603D)另列 | 同上；遞送endpoint/auth/request params未驗證       |

付費商品有合法官方來源，不能寫成「不存在官方分點資料」。但商品頁不是已取得的資料契約，內用訂閱不能直接做公開網站；不自行購買或推測付費下載URL。歷史訂購可分單、商品單次訂購年限不等於免費API歷史上限。

分點資料包含券商自營與經紀客戶成交；總公司可能承接自營量，不能識別個人或當成一位「主力」的交易。TPEx熱門股活躍券商排行不是全股票完整分點資料。未來若有授權，還需券商代碼／組織歷史、session與範圍、Decimal價格、raw/revisions及archive安全設計；下載EXE若為自解壓資料，必須安全檢查／解出資料，**不得執行下載的二進位程式**。

## 7. 查證證據與尚未解決事項

稽核暫存樣本在`/tmp`，沒有加入repository或當成正式seed；TDCC session HTML可能含CSRF token，**不得提交原HTML／cookies／token**。可重現查證依官方URL、實測日期、回應shape及下表觀察值，不把raw大型檔案塞入spec。

| 少量驗證              | 實際結果                                                             |
| --------------------- | -------------------------------------------------------------------- |
| TWSE／TPEx官方Swagger | HTTP200；確認真實路徑與未宣告日期參數                                |
| I1兩個日期            | 2016-11-07與2026-11-06成功，16／19欄                                 |
| I2／I3                | latest OpenAPI20欄；dated16／24欄；現行I3全5,062列完成算術與分類對帳 |
| O1／O2                | O1全檔CSV與hash驗證；O2只驗證前綴及實際BOM key／日期，不稱全檔檢查   |
| O3                    | 一次正常session GET/POST查得2330／2025-11-07，未超出頁面提供日期     |
| M1、M5、M6、M8        | 四份live JSON，核對日期缺失／ROC日期／張股差異                       |
| M2、M3、M4、M7        | 四份2016-11-07 JSON，核對回傳日、fields/groups、單位、市場別         |
| 分點                  | 只讀公開入口與官方商品metadata；未取得受限報表，無CAPTCHA／帳號操作  |

必須進一步確認：

1. **權限**：I1/I3與各網站歷史匯出自動下載、保存、對外服務的適用條款；有權限來源可先做，未確認來源預設disabled。不能承諾Phase2B已具備完整雙市場法人明細。
2. **歷史契約**：2016外資分類到現行schema的生效界線、完整日期覆蓋與歷史證券身分；TDCC一年以前沒有已確認官方批次來源。
3. **日期／單位**：M1可信交易日，M8金額單位與授權，非普通股張數換算，TDCC不同表示法adjustment方向。
4. **尚未完成body驗證的路徑**：TWSE Swagger已列`/SBL/TWT96U`；它的實際資料／日期／scope仍需獨立查證，不拿其他借券表代稱已完成。
5. **發布與限流**：數值quota、精確公開API刷新時間、TDCC週資料metadata月頻率矛盾、CSV/JSON lag；目前只能保守建模，不作SLA或完整性承諾。
6. **分點**：官方授權預算、內外用範圍、遞送契約與歷史保留。未解決前維持Deferred，不納入2B–2F硬性完成要求。

本次沒有開始2B、沒有新增migration、沒有改UI、沒有commit／push／部署。下一階段的工作與權限條件列於roadmap。
