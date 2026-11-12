# Phase 2H-1 — 券商分點資料來源查證

查證日期：**2026-11-13，Asia/Taipei**。本次只做Repository Review、公開官方／供應商文件查證及指標契約；搭配[指標定義](phase-2h-metric-definitions.md)。沒有提交驗證碼、登入、購買、接受訂購條款、呼叫受限資料API、取得正式報表、執行EXE、修改資料庫／Worker或進入2H-2。

**結論：存在官方分點資料商品與有文件的程式遞送方式，但本專案尚無已核准的分點資料自動下載、公開展示及衍生利用契約；正式來源全部維持Blocked／disabled。** 文件可支持設計，不等於已取得資料或權利。合法保存的離線CSV也必須有可核對的來源／使用證明，不因上傳就變成可公開資料。

## 1. Repository Review、可重用部分與本階段計畫

已閱讀AGENTS、README、[Phase 2A來源](phase-2a-data-sources.md)、[Domain規格](phase-2-domain-model.md)、[Phase 2驗收](phase-2-final-validation.md)、[Dashboard規格](phase-2g-chip-dashboard.md)，並檢查現有業務模組、公用Port、圖表／Client與0001～0010 Migration鏈。

| 現有內容 | 後續可沿用 | 必須另外設計，現在沒有實作 |
| --- | --- | --- |
| `market_data.public`的Instrument Identity／Reference Readers | Instrument UUID、市場／日期語意與公開查詢邊界 | 券商／分點歷史身分、代碼重用、合併／停業有效期；不能用證券主檔代替 |
| Institutional／Ownership／Margin／Lending匯入模式 | Source Manifest gating、Raw-first、Item Audit、原子報表、不可變Revision、Promotion／Replay政策的模式 | 分點價格級距列、報表完整性、Branch Mapping與各付費交付契約；不能直接複製另一報表的Parser／公式 |
| `technical`／`chip_analysis` | 50位Decimal、12位HALF_EVEN、Input Revision／As-of／缺值語意及共用Calculator架構 | 新的版本化Broker Metric；既有封閉Metric Enum／Institutional Category不是分點資料的別名 |
| 公開Readers與Query組裝 | 同一RR READ ONLY Snapshot、查詢上限、Pydantic／OpenAPI測試模式 | 新的Broker Reader／API Contract，不能跨模組讀私有ORM |
| `chip-api.ts`／`chip-types.ts` | 同源Proxy、Abort／Timeout、Runtime Guards、精確字串／Coverage／Lineage型別模式 | Broker-specific Response Guard；目前沒有Broker API或Proxy allowlist |
| `chip-dashboard.tsx`／`chip-chart.tsx`／`stock-chart.tsx` | 已造訪Tab掛載、Loading／Empty／Error／Partial、圖表生命週期／ResizeObserver／Viewport／缺值分段、LWC日資料與副圖 | 新Tab、排行榜及跨圖日期十字線同步；現有圖表游標彼此獨立，不能宣稱已共用 |
| 既有Migration與`daily-worker` | Schema審查／隔離測試及顯式啟用模式 | 沒有Broker Table／下載Job；本次不修改Source allowlist或執行排程 |

本次Implementation Plan：①查官方入口、商品及條款；②分開整理文件／實際Body／歷史／單位／交付與權利矩陣；③定義可重現的公式、版本、參數與SYNTHETIC數值規格；④檢查文件一致性與既有回歸後停止。2H-2～2H-7僅是後續授權範圍，不在本次實作。

## 2. 驗證標記及來源對照

- **文件已驗證**：本次讀到官方頁／格式文件，不代表實際資料回應通過Parser。
- **歷史目錄**：供應商宣稱可供應的起點，不代表本專案持有、每個交易日完整或可透過免費API取得。
- **Body未驗證**：尚無合法實際報表；字元編碼、Literal Schema、日期、版本、完整度仍需授權樣本。
- **Blocked**：缺專案權利、交付契約或必要資料語意；下載／公開展示維持disabled。
- 下列H-B編號是**規格索引**，不是已建立的DataSource UUID／Adapter或已啟用Manifest。

| ID | 來源／正式入口 | 可確認的交付／歷史目錄 | 專案狀態 |
| --- | --- | --- | --- |
| H-B1 | [TWSE公開BSR](https://bsr.twse.com.tw/bshtm/bsWelcome.aspx) | 公開入口GET；當日查詢、驗證碼／安全限制；實際資料Request未驗證 | **Blocked**自動抓取／資料展示 |
| H-B2a | [TWSE全部、不含權證](https://eshop.twse.com.tw/zh/product/detail/34e615c34c4e4ca3b23fc732bd736f94) | Daily ZIP、2012-10-25起目錄；線上／Email交付選項 | **Blocked**訂閱／交付／公開及衍生利用 |
| H-B2b | [TWSE全部、含權證](https://eshop.twse.com.tw/zh/product/detail/c862b8472d7d46ccafbecca13c0336b0) | 獨立Daily ZIP商品，同起點；不可延伸H-B2a權利 | **Blocked**，不與普通股商品混合 |
| H-B3 | [TPEx一般查詢](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/brokerBS.html)／[鉅額查詢](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/brokerBS-block.html) | GET表單；當日、逐證券非機器人驗證，CSV控制項 | **Blocked**自動抓取／轉載 |
| H-B4a | [TPEx不含權證商品](https://eshop.tpex.org.tw/zh/product/detail/E982FB3B7484B8AD66AF83E6F31F0CDB) | `STKRPQSTK.EXE`／`STKGMHSTK.CSV`；2014-12-01起目錄 | **Blocked**；有授權API文件但未取得實際契約 |
| H-B4b | [TPEx權證商品](https://eshop.tpex.org.tw/zh/product/detail/23F0FF130FA5C09FB1FD7046AFFA603D) | `STKRPQWRNT.EXE`／`STKGMHWRNT.CSV`，獨立商品 | **Blocked**；不複用股票Parser／權利 |
| H-B5 | [FinMind分點文件](https://finmind.github.io/tutor/TaiwanMarket/Chip/) | 訂閱API，文件列2021-06-30起、單日查詢 | **Blocked**本專案下載／對外展示；未訂閱或驗證資料 |
| H-B6 | [TEJ分點研究](https://www.tejwin.com/insight/%E9%81%B8%E8%82%A1%E5%9B%A0%E5%AD%90%E7%A0%94%E7%A9%B6%E5%88%B8%E5%95%86%E5%88%86%E9%BB%9E%E7%B5%90%E5%90%88%E5%8B%95%E8%83%BD%E5%9B%A0%E5%AD%90%E4%B9%8B%E7%A0%94%E7%A9%B6/)／[通用REST文件](https://api.tej.com.tw/document_rest.html) | 商業資料候選；一般Datatables認證介面有文件，分點Table ID／產品契約未取得 | **Deferred**；不猜分點Endpoint或歷史範圍 |

## 3. TWSE公開與授權商品

### H-B1 公開查詢

普通GET到BSR入口得到雙語安全阻擋內容；瀏覽工具未提供可核對的狀態碼，因此不將它寫成實測HTTP403。沒有送表單或驗證碼。官方索引將其描述為營業日當日查詢，經紀與自營交易可能同屬報表；一般／零股與鉅額有不同產製時段，但這次沒有取得報表，完整欄位／Scope／日期格式／實際下載URL皆未驗證。[官方入口](https://bsr.twse.com.tw/bshtm/bsWelcome.aspx)、[投資人知識網](https://investoredu.twse.com.tw/pages/TWSE_investor_library_Index_7.aspx?ID=3)

這不是十年歷史API。GET入口的成功／失敗也不是資料授權。[TWSE網站使用條款](https://www.twse.com.tw/zh/terms/use.html)另區分同意下載方法與特定開放資料；未確認本報表有OGDL或自動批次／再傳輸權。

### H-B2a／b 付費商品Metadata

| 項目 | 不含權證 | 含權證 |
| --- | --- | --- |
| 頻率／格式 | 每交易日，ZIP；內部檔案未解析 | 每交易日，獨立ZIP；未解析 |
| 一般／鉅額產製時刻 | 16:10／17:30 | 16:00／18:00 |
| 目錄起點 | 2012-10-25 | 2012-10-25 |
| 文件欄位 | 證券代號、證券商、成交單價、買進股數、賣出股數 | 同一層級Metadata欄位，不證明Literal Schema相同 |
| 股數／金額 | 數量欄明示股；沒有獨立買／賣金額欄 | 品種基礎單位、價格scale／幣別仍需正式規格 |
| 分點 | Metadata的「證券商」不足以證明代碼、總公司／分公司及名稱欄如何分拆 | 同左；不能以名稱、前綴或位置猜映射 |
| 已知交易範圍 | 一般與鉅額分檔；零股／盤後及錯帳更正基礎仍需確認 | 含權證是不同Instrument／Scope Profile |
| 遞送 | 文件提供線上下載及Email原檔／轉檔選項；實際認證URL、Method、參數及自動化額度未知 | 同樣未取得自動遞送契約 |
| 統計日期／發布時間 | 實際內檔日期與版本尚未驗證；表列產製時間不是每份檔案發布證據 | 同左 |

以上依據兩個[不含權證](https://eshop.twse.com.tw/zh/product/detail/34e615c34c4e4ca3b23fc732bd736f94)／[含權證](https://eshop.twse.com.tw/zh/product/detail/c862b8472d7d46ccafbecca13c0336b0)商品頁。頁面備註對總公司自營量歸屬及純自營商／證券期貨自營商有排除定義；不可由「全部」推論涵蓋所有投資人或所有市場成交。公開ZIP樣本連結由瀏覽工具回報格式不支援，這次沒有解析，不能把Metadata當成Fixture驗收。

官方[FAQ第5、14題](https://eshop.twse.com.tw/zh/page/faq.html)另有兩項要釐清：一般線上訂購流程描述近半年，商品頁則列更早庫存與單筆最多5年；這是目錄／訂購通路差異或文件衝突，不能宣稱驗證了跨十年自動遞送。產製時間亦會因交易量／券商申報而延後，不能當SLA或補成`source_published_at`。

### 權利Gate

[官方訂購條款](https://eshop.twse.com.tw/zh/shopping/finishOrder?show=true&showTIP=)區分內用、對契約資訊用戶的外用、來源及訂單標示、再傳輸限制，並另外要求取樣編製指數或其他衍生商品取得事前書面同意。因此本專案不能自行認定外用訂閱已包含匿名公開網站、開放API、排名／Proxy等所有衍生展示；須將擬議用途交由供應方書面確認。這是啟用Gate，不是主張所有內部算術運算均被禁止。

## 4. TPEx公開查詢、格式與授權遞送

### H-B3 公開查詢

[一般交易頁](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/brokerBS.html)明示僅提供當日上櫃證券資訊、每證券查詢／下載須非機器人驗證，並提供Big5／UTF-8 CSV按鈕與16:00更新說明；另有[鉅額頁](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/brokerBS-block.html)。沒有提交代碼或驗證資料，實際Report Method、Token、欄位與下載Response均未驗證。公開頁的資料散布／販售限制不能被CSV按鈕抵銷，亦不證明歷史API或自動利用權。

### H-B4 商品與格式文件

不含權證商品每日一般檔`STKRPQSTK.EXE`標16:00（UTC+8），鉅額檔`STKGMHSTK.CSV`標17:15；權證商品使用`STKRPQWRNT.EXE`／`STKGMHWRNT.CSV`，是另一份契約。目錄均列2014-12-01起；訂閱期選項不是已驗證的歷史涵蓋保證。[股票商品](https://eshop.tpex.org.tw/zh/product/detail/E982FB3B7484B8AD66AF83E6F31F0CDB)、[權證商品](https://eshop.tpex.org.tw/zh/product/detail/23F0FF130FA5C09FB1FD7046AFFA603D)

官方[收市後交易資訊格式PDF](https://www.tpex.org.tw/storage/regular_system/%E6%96%B0%E7%89%88%E6%94%B6%E5%B8%82%E5%BE%8C%E4%BA%A4%E6%98%93%E8%B3%87%E8%A8%8A%E6%A0%BC%E5%BC%8F%E8%AA%AA%E6%98%8E%28V1.33%E7%89%88%29.pdf?t=20251127)封面為 **v1.33／2025-10-20**，不能沿用搜尋標題的v1.32。印刷頁114～115的S32／S33可確認：

| 項目 | 文件可確認 | 尚未確認 |
| --- | --- | --- |
| 報表日期／頻率 | 日報；Header西元YYYYMMDD、產生時刻HHMM及筆數 | 產生時刻不是自動等於官方發布時間；歷史版本生效範圍與實際Body未驗證 |
| 身分 | 證券代號、證券商代號、名稱 | 親子券商／分點映射、Code寬度／大小寫／歷史更名與合併 |
| 數值 | 成交單價（元）、買／賣股數；鉅額另有配對／逐筆類別 | 價格ISO幣別／scale、價格列粒度、獨立金額未確認 |
| 交易Scope | S32一般、S33鉅額，不能重複相加 | 零股／盤後及錯帳更正納入範圍；「一般」不可自行展開 |
| 格式 | S32為自解壓CSV封裝，S33 CSV | 股票文件不能直接當權證檔Literal Schema；正式CSV編碼與完整實檔待核 |

**不得執行EXE／自解壓程式。** 後續至少採供應方合法提供的純CSV／非可執行ZIP；若只有EXE，應先釐清可用交付，不自行執行或把副檔名改成ZIP就接受。文件變更記錄也曾停用舊S14／S39（2018-05-14）；不恢復舊路徑或假定2014起都使用v1.33。

### 已找到API遞送文件，但仍未取得契約

相較Phase 2A，新增證據是[官方API商品入口](https://eshop.tpex.org.tw/zh/product/detail/2c92e01394fcf4c7019518bbf65f000a)連到[API DOCX手冊](https://eshop.tpex.org.tw/uploadFile/upload/product/2c92e01394fcf4c7019518bc880f000b.docx)（2024-09-02）。本次只讀取公開文件文字，不呼叫資料API。

- 文件列**相對路徑**`/download/getApi?step=0`取得訂閱檔案列表、`step=1`取得選中檔案；step0為文字列表，step1為訂閱檔案本身。
- 參數`account`、`pwd`、`lang`，step1另有列表提供的`fileName`；API帳號與網站登入不同。
- **文件矛盾**：參數表稱POST，範例卻使用GET／Query Credentials，Base URL是佔位符。故HTTP Method／絕對Endpoint／安全認證傳遞尚未核實；不拼出猜測URL、不把密碼放進URL／Log。
- 文件有API啟用、商品API資格、訂閱到期、每檔下載次數與有效期檢查。沒有任意歷史日期參數；不能由檔名推論日期查詢API。

API手冊公開或其文件商品免費，不代表分點資料免費、產品一定可用API或已取得自動同步權。需要正式帳號、產品對應、安全Method、配額、回補／修訂流程及本專案使用契約才能實測。

[TPEx申購條款](https://eshop.tpex.org.tw/zh/product/shoppingTerm)同樣區分內外用、指定資訊用戶與再傳限制，並對重製／散布及指數／其他衍生商品另設書面同意要求；[商店網站條款](https://eshop.tpex.org.tw/zh/useTerms/index)也規範同意的下載方式。沒有把付費外用解釋為本平台可任意公開Raw／排行榜／Proxy。主站／免責頁部分讀取403，沒有繞過或換代理。

## 5. 其他供應商候選

### H-B5 FinMind

文件列`GET https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report`，以Bearer認證，`data_id`或`securities_trader_id`與`date`查單日；JSON有`date/stock_id/securities_trader_id/securities_trader/price/buy/sell`，數量為股。文件起點2021-06-30、平日18:00更新說明，非本專案實測涵蓋／發布時刻；文件排除鉅額，零股／盤後、幣別及實際Body仍待核。分點簡稱不能代替ID，均價版也不能反推精確成交額；不能自行補平來源買賣差額。[供應商資料文件](https://finmind.github.io/tutor/TaiwanMarket/Chip/)

該供應商的[授權說明](https://finmind.github.io/Disclaimer/)明確限制分點資料供訂閱者自行／內部使用，所有方案均不能由此直接取得向第三人展示權。[Commercial Q&A](https://finmind.github.io/CommercialUse/)提及使用者自備帳號方式，但不能由此自行設計共享Token的公開Dashboard或認定與明文限制已完全相容；仍需書面釐清。本專案無訂閱／Token／資料請求，維持disabled。

### H-B6 TEJ

供應商自身有分點資料研究與一般[認證Datatables API](https://api.tej.com.tw/document_rest.html)文件；本次未取得分點專用Table ID、欄位、歷史契約、展示／衍生權。分點日期／頻率、代碼／名稱、價量／單位／Scope及實際遞送格式均未核實；通用JSON／XML API不代表已驗證分點Response。一般API存在不等於分點商品對本專案可用；不猜Dataset Code或用研究回測期間宣稱可購歷史深度。

## 6. 資料品質與身分最低契約

1. `statistics_date`必須來自經驗證的報表Header／官方欄位，採市場本地交易日；`source_generated_at`、`source_published_at`、`first_seen_at`、`ingested_at`分開，未知不補UTC午夜或下載日期。
2. 股票使用既有Instrument UUID；分點Code保留原字串／大小寫／前導零，禁止名稱模糊比對、任意截前綴或將目前券商名錄當完整歷史。還需核對Broker、Branch及有效期／市場／來源Namespace、合併或代碼重用事件。
3. 先確認價格級距／一般／零股／鉅額／更帳基礎與原始單位，再按同一Identity／Date／Scope彙總；已有「股」不再乘1000，張數換算須有有效期Evidence。價格或金額採Decimal／明確Currency，不以全台股一律TWD。
4. 缺列不自動當0；只有同份完整報表且來源明定省略即無成交，才可建立可驗證的零交易語意。未知分點／拒絕列保留Audit；不能只刪掉壞列就宣稱完整TOP15。
5. 不要求所有來源的全市場買賣數強制平衡；只有Source Contract證明參與者與Scope對稱時才能做相應總計對帳。分點報表不必等於含不同管道的DailyBar成交量，禁止補平或用錯誤分母算集中度。
6. 保存合法Raw／Hash、Item Row Locator、Source／Contract／Schema／Parser／Identity Version、不可變Revision及Promotion Evidence；同日不同Scope／來源不拼接、舊Replay不覆蓋較新合法修訂。實際規則與Schema待2H-2審查。
7. 分點彙總的是不同客戶及可能的自營交易，不是個人身分或真實庫存。價格級距總量也不是單筆委託／單一投資人的交易規模；中實戶／大戶分類維持unsupported。

## 7. 權利與能力的啟用Gate

後續Source Manifest至少應分開記錄：取得／自動同步、Raw保留、Revision留存、內部計算、原始資料展示、衍生指標展示、對外API／再散布，以及各權利的對象、有效期與書面Evidence。這是**待實作契約**，本次沒有增加Manifest或自動打開任何權利。

目前沒有核實官方數值QPS、完整歷史連續性、資料更正序列／撤回程序或每份發布SLA；未知不填成不限流／十年完整。既有工程5秒間隔／有限Retry不是交易所授權。登入、驗證碼、403、訂閱與檔案到期一律遵守，不輪換IP、偽造Token、購買帳號或用非官方網站補成真資料。

## 8. 本次結果與下一子階段條件

本次取得／匯入的正式分點資料：**0個交易日、0檔證券、0筆交易列**。沒有執行正式DB盤點，以上只指本次可驗收覆蓋，不把既有日行情／法人／TDCC或供應商範例算成分點資料。沒有建立`broker_flow`、Migration、CLI、API、Tab或`phase-2h-final-validation.md`，也沒有改每日Worker。

在**另外授權2H-2後**可依本規格與[指標定義](phase-2h-metric-definitions.md)設計純Domain／Schema；若權利仍缺，僅限明示SYNTHETIC的離線開發／測試，不解除正式資料Blocked。正式2H-3～2H-7至少需要：

- 選定股票／權證、市場與Scope，供應方書面確認預定內部、公開Raw、排名／Proxy、使用者與衍生用途。
- 合法實際CSV／安全ZIP、日期／編碼／價量幣別／Literal Schema版本、歷史起點與缺日／修訂清單；TPEx EXE另需安全非執行交付方案。
- 券商／分點身分及歷史事件資料、合併／停業／代碼重用政策，與證券日期身分Mapping。
- 已核實交易日曆、原始交易Scope／分母與零列語意；解析精度、TOP N Tie-breaking、完整度及Proxy命名簽核。
- 安全API認證／絕對URL／Method／配額及記錄遮蔽；不能將TPEx文件的GET／POST衝突自行猜定。

本子階段交付只限兩份Spec，完成後停止；不購買、commit、push、部署或自行開始2H-2。

## 9. 驗證紀錄

| 檢查 | 本次結果 |
| --- | --- |
| 官方／供應商資料 | 查證上述公開Metadata／條款／格式文件；沒有受限API、登入或正式資料Body驗收 |
| 指標規格V1～V3 | 使用獨立50位Decimal驗算通過；V4為待後續實作的邊界驗收規格，不當作Broker引擎單元測試已通過 |
| 既有後端離線Unit | **2,634 passed、499 deselected**，7.07秒；1個既有Starlette deprecation warning |
| 既有Vitest | **352 passed／20 files** |
| Typecheck／Production Build | 通過，沒有部署或啟動新服務 |
| 文件／Git | 本地文件連結、公式範例及`git diff --check`檢查；最終只新增本來源Spec與指標Spec |

本次只改文件，因此不執行DB／Migration／Provider Network／Playwright或不存在的新Importer測試；沒有把略過項目當通過。數值驗算記錄與既有測試Log位於已忽略的`.tools/validation/phase-2h1/`；公開Metadata讀取不等於正式來源End-to-End通過。Next Build自動產生的next-env變動已還原。
