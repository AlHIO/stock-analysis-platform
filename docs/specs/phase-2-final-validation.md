# Phase 2G-2 — Phase 2 System Integration & Acceptance

驗收日期：**2026-11-12，Asia/Taipei**。範圍：Phase 2B～2G，含Phase 1行情／技術指標回歸。本次不新增分析功能、來源或正式資料，不進入Phase 3。

**結論：軟體整合與回歸通過；正式籌碼資料覆蓋仍為 Partial，未核准來源與完整法人／借券未還驗收為 Blocked。** 此結論不等於全市場、十年歷史、Point-in-Time回測或公開發布已就緒。效能數字為本機量測，不能取代正式負載／容量驗收。

## 1. 完成功能與驗收邊界

| 模組 | 已有功能與本次驗證 | 正式資料驗收 |
| --- | --- | --- |
| Market Data | 多市場Instrument UUID、Canonical日行情／Revision、日週月K、歷史工具、搜尋及來源追蹤 | 現有真實行情讀取／日期範圍／手機桌面回歸；不宣稱區間連續完整 |
| Technical | SMA／EMA／RSI／MACD／Volume SMA、參數／暖機／raw price、共用Decimal引擎 | 數值回歸、真實歷史API與瀏覽器；未提供adjusted price／回測保證 |
| Institutional | Domain、Persistence、Adapter、raw-first匯入／Replay／Audit、History／Latest／5/10/20/60-session／Streak API | 軟體全流程及隔離DB通過；正式Canonical為0，完整真實法人流程 **Blocked** |
| Ownership | TDCC週報、17級距／Scheme、Raw／Audit／Revision／Canonical、CLI、分布／大戶比例／週差API | 2330一週的真實值通過；歷史與連續週差 **Partial** |
| Margin | 融資／融券雙Ledger、日期／單位Evidence、Raw／Revision、CLI、History／Latest／日增減 | 6488一日真實資料通過；全市場／歷史 **Partial** |
| Lending | 借券賣出與借券未還分開、Channel／Scope／Quota、Raw／Revision、CLI及API | M6的6488一日數值通過；Scope／Channel未核實，借券未還 **Blocked** |
| Chip Analysis | 版本化跨來源Engine／API、公開Readers、RR Snapshot、As-of／Input Revision／完整度 | 真實Margin／Ownership可計算值通過；Lending受阻、法人空資料正確回狀態 |
| Dashboard | 四個Tab、日期區間、價格／指標保留、週分布、大戶比例、融資／借券圖、來源與完整度、響應式 | 真實Canonical與同源Proxy整合，另以SYNTHETIC測UI狀態；沒有通用跨來源Widget |

Dashboard使用原生產品API；通用`chip-metrics`於HTTP與Playwright測試程式中另外驗證，沒有把它說成已新增的跨來源畫面。所有金融公式仍由後端共用Calculator／Engine負責。

## 2. 自動化與系統測試結果

| 檢查 | 實際結果 |
| --- | --- |
| 完整後端Pytest | **2,984 passed、4 network deselected**，338.93秒；1個既有Starlette TestClient deprecation warning |
| PostgreSQL／Redis Integration與Migration | 包含於上列的 **472 cases**，其餘 **2,512** 為Unit／非整合案例；沒有把重複執行數相加 |
| OpenAPI | Source export及執行中Backend `/openapi.json`與Snapshot深度一致，**23 paths** |
| 公開GET Smoke | **23個路由皆200**；16個Instrument路由查不存在UUID皆404；31組非法／超限條件皆422，安全錯誤檢查通過 |
| Domain分層 | AST檢查 **56個Domain Python檔**，無FastAPI／SQLAlchemy／Redis／HTTP／供應商套件相依；公開Port／Facade回歸通過 |
| Docker與Migration現況 | 四個必要服務healthy；Current=Head=`0009_lending`；`alembic check`無差異 |
| 完整Vitest | **352 passed／20 files**，包含既有行情／指標與籌碼UI狀態 |
| Typecheck／Production Build | 通過；Next.js 16.4.5／Webpack、TypeScript與頁面產生成功 |
| 完整Playwright | **18 passed，21.5秒**；含2個新增真實Canonical案例，沒有skip；另有明示SYNTHETIC的UI狀態／指標數值案例 |

Playwright對本次Build的暫時本機Production standalone server驗收，不以舊Docker前端映像替代。涵蓋股票代碼／名稱搜尋、日週月K／2016歷史日期、指標參數／暖機／數值、四個籌碼Tab、股價與籌碼Canvas保留、週資料／級距日期、桌面1440px／手機390px、Loading／Error／Empty／Partial／Stale。真實2330／6488測試不攔截API、不用Fixture；通用跨來源API由測試程式核對Input Revision，瀏覽器本身仍只走同源Proxy。

四個network案例未執行：兩個TPEx法人live、TWSE Quotes與TPEx Quotes；沒有將deselected或Mock當作真實來源成功。本次官方數值抽樣使用已保存且有Hash／日期／Source Lineage的官方回應，另重新閱讀官方來源及授權Metadata；沒有新下載行情／籌碼或新建正式Canonical。

### 隔離與安全性

後端使用既有`run_institutional_validation.py`建立獨立臨時資料庫`stockapp_p2b6_test_b35314e6ea5a4428`，每個寫入測試再使用私有隨機Schema；執行完已刪除該臨時DB。正式`stockapp/public`只做RR READ ONLY查詢，沒有執行Upgrade／Downgrade／測試Replay或停止、重啟Container／History Worker。

實際案例包含：空Schema Upgrade→Head、Metadata parity、空資料Downgrade／Re-upgrade、非空Downgrade拒絕且不部分刪表、PK／FK／CHECK、重複匯入、跨Worker並發、Revision Promotion、舊Raw不得倒退Canonical、Rollback、Checkpoint／Resume、不同Source／Contract／Taxonomy／Scope／Channel隔離。

查詢涵蓋：404／422／503、空資料、Partial／缺交易日／缺週、不同發布／觀察／匯入截止時間、修訂Input、精確Decimal字串、查詢上限／Cursor、GET不讀Raw body／不下載供應商，以及跨產品共享RR Snapshot的並發行為。503／來源失敗等以隔離測試注入，不為驗收關閉正式PostgreSQL或來源。

## 3. Database完整性與Phase 1保護

正式PostgreSQL **17.11**；Alembic Current／Head皆 **0009_lending**，`check`回`No new upgrade operations detected.`，不需要新Migration。

| 正式Schema | PK | UNIQUE | FK | CHECK | Index數 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Institutional | 6 | 9 | 16 | 94 | 29 |
| Ownership | 9 | 10 | 18 | 51 | 32 |
| Margin | 6 | 6 | 13 | 43 | 23 |
| Lending | 7 | 6 | 15 | 39 | 25 |

另有2個Ownership constraint trigger。整個public Schema共147個Index；**0個未Validated Constraint、0個Invalid／Not-ready Index**。四個籌碼Canonical對Revision的孤兒及數量單位不一致均為0；Raw Body SHA-256、Raw→Run／Source、Canonical→Revision／Observation及Instrument UUID核對通過。

Canonical只指向政策允許的完整報表修訂；Revision按來源／契約／分類／Scope等相容Profile保留。不同來源同日資料不是直接相加或以最後寫入取勝，並發／過期Replay／不相容Promotion由上述行為測試驗證。正式冪等性驗收不重寫正式資料，重送與Rollback在隔離DB完成。

Phase 1驗收前後皆為 **9,712,317 DailyBars、9,739,041 DailyBar Revisions**。2330／6488各取最早20＋最新20筆，共80個有界樣本，內容皆有相符Revision；前後樣本Hash完全相同。22項受檢主檔／行情／籌碼／Raw／Audit表的筆數完全相同，三個籌碼Canonical指標Hash也相同。這是筆數、約束及有界樣本驗證，**不是對970萬列做逐筆全表Checksum**。

## 4. 真實資料覆蓋與官方抽樣

| 產品 | Canonical／Revision | Raw／Item Audit | 真正統計日與範圍 |
| --- | --- | --- | --- |
| Institutional | 0／0 | 0／0 | 正式DB尚無，不以隔離測試或早期臨時DB的Raw充數 |
| Ownership | 1／1 | 2／8,230 | 2330，2026-11-06，**一檔一週** |
| Margin | 1／1 | 4／4,063 | 6488，2026-11-06，**一檔一日** |
| Lending Short Sales | 1／1 | 3／2,799 | 6488，2026-11-09，**一檔一日** |
| Borrowed Positions | 0 | 無可用Canonical | 來源未啟用，不以借券賣出替代 |

2個Market、55,223筆證券主檔、11個DataSource。`trading_calendars`為 **0筆**，目前缺少正式已核實交易日曆，不能因此顯示完整法人連續交易日統計；週曆／來源發布SLA也未完整核實。

儲存的三份Revision欄位完整度雖為`complete`，只描述該報表可用欄位，不代表Source Scope、時效、全市場或歷史完整。尤其M6的數值Identity可核對，Channel／Scope仍未核實，跨來源Engine應維持`not_verified/null`。

### 已保存行情範圍

| 市場／類型 | 有行情證券數 | DailyBars | 最早日期 |
| --- | ---: | ---: | --- |
| TWSE股票 | 1,089 | 3,928,719 | 2007-07-27 |
| TWSE ETF | 245 | 352,814 | 2007-07-27 |
| TWSE ETN | 14 | 14,329 | 2019-04-30 |
| TWSE權證 | 38,266 | 1,742,750 | 2014-07-31 |
| TWSE特別股／DR／REIT | 27／10／6 | 42,466／38,750／21,948 | 2007-07-27 |
| TPEx股票 | 897 | 2,896,275 | 2007-07-27 |
| TPEx ETF | 119 | 158,375 | 2011-01-27 |
| TPEx ETN | 6 | 4,855 | 2019-04-30 |
| TPEx權證 | 12,063 | 509,628 | 2024-11-26 |
| TPEx特別股 | 1 | 1,408 | 2020-02-24 |

上述各組最新已存日期皆2026-11-05。最早／最新端點與證券數不證明中間交易日、已下市證券、所有商品或十年資料完整；更不能拿日行情的歷史範圍宣稱籌碼資料也有相同歷史。

### 官方封存回應 → DB → HTTP逐欄核對

沒有重新下載最新快照冒充2026-11-06／09；使用原始Official Raw的SHA-256、Source Endpoint與IngestionRun連結核對。

1. **TDCC 2330，2026-11-06**：六欄CSV的17個級距逐一核對人數／股數／官方比例與DB／API。官方集保存量 **25,932,370,067股**、持有人數 **3,259,221人**；400001股門檻的級距股數 **22,563,646,397股**，以同份官方分母獨立Decimal驗算為 **87.009580453709%**。Adjustment／Total不重複加總；週差null。
2. **TPEx M5 6488，2026-11-06**：10個來源數量欄位全部相符。官方融資10,285張→**10,285,000股**、融券317張→**317,000股**，僅使用這檔／這日已審查的1000係數Evidence。融資日差 **−1,128,000股**、融券 **+119,000股**；Signed Adjustment為null／not_applicable，沒有填0。
3. **TPEx M6 6488，2026-11-09**：5個借券賣出欄位全部相符。前餘額 **12,476,000**＋賣出 **348,000**−還券 **360,000**＋調整 **0**＝餘額 **12,464,000股**。該0為官方reported，不同於M5的不適用。`Sale*`融券欄未混入借券Ledger；借券未還沒有被推估。

三者官方發布時刻均未知，保持null；統計日期分別來自CSV `YYYYMMDD`／JSON民國日期，不用驗收或下載日期代替。API／DB／Raw與Input Revision可追溯；核對Artifact為`official-archive-comparison.json`。

## 5. 來源能力與授權狀態

本次重新閱讀官方Metadata／條款的日期為2026-11-12。授權只適用該資料集／交付路徑，不將OpenAPI開放狀態延伸至其他網站匯出。本次沒有新增使用許可或啟用disabled來源。

| Source | 實際交付路徑 | 已核實能力／使用狀態 | 驗收 |
| --- | --- | --- | --- |
| I1 TWSE法人T86 | GET `https://www.twse.com.tw/fund/T86` | 使用權pending；僅兩個已稽核日期Parser，不推論連續歷史 | **Blocked** |
| I2 TPEx法人 | GET `https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading` | dataset11856 OGDL；手動opt-in、latest-only；20欄缺完整dealer、Scope未核實 | **Partial Raw／Blocked完整Canonical** |
| I3 TPEx法人日期匯出 | GET `https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade` | 非已核准交付路徑，歷史分類邊界未核實 | **Deferred／Blocked** |
| O1 TDCC CSV | GET `https://opendata.tdcc.com.tw/getOD.ashx?id=1-5` | dataset11452 OGDL；latest-only，六欄／17級距v1，需dated identity | **Partial** |
| O2 TDCC JSON／O3 HTML | `https://openapi.tdcc.com.tw/v1/opendata/1-5`／`https://www.tdcc.com.tw/portal/zh/smWeb/qryStock` | JSON與CSV舊稽核有日期差；HTML批次權限未確認，不宣稱十年歷史 | **Deferred／Blocked** |
| M1 TWSE Margin OpenAPI | GET `https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN` | 可手動Archive，latest-only且無交易日期；Canonical需同Payload可信日期與張股Evidence | **Partial／缺Evidence時Blocked** |
| M2 TWSE dated Margin | GET `https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN` | 網站使用權／分組Schema未核實，disabled | **Blocked** |
| M5 TPEx Margin | GET `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance` | dataset11387 OGDL；latest-only、民國日期、21欄、原單位張，Quota可能次營業日生效 | **Partial** |
| M6 TPEx Lending Short | GET `https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl` | dataset11632 OGDL；latest-only、16欄、原單位股；Channel unmapped／Scope unspecified | **Partial／跨來源分析not_verified** |
| M3／M4／M7 | TWSE `TWT93U`／`TWT72U`、TPEx dated `margin/sbl` | 網站使用權／群組Schema／Channel未核實；借券未還不得由M6代替 | **Blocked** |
| M8／M9 | TPEx `tpex_short_sell`／TWSE `SBL/TWT96U` | 使用權或資料集映射／單位／日期／Quota生效日不足，disabled | **Blocked** |
| 券商分點 | Phase 2A候選官方查詢／授權商品 | 無已核准、可維護的自動交付契約，不繞CAPTCHA | **Deferred** |

官方依據：[TDCC 11452](https://data.gov.tw/dataset/11452)、[法人11856](https://data.gov.tw/dataset/11856)、[Margin 11387](https://data.gov.tw/dataset/11387)、[Lending 11632](https://data.gov.tw/dataset/11632)、[OGDL條款](https://data.gov.tw/license)、[TWSE使用條款](https://www.twse.com.tw/zh/terms/use.html)。TPEx條款頁本次403，沒有繞過；授權確認保留上述具名dataset證據。TDCC目錄仍標月更、來源為週統計的衝突未解；Metadata列完整dealer也不能取代實際20欄Response契約。沒有核實官方數值QPS／發布SLA；5秒間隔／3次Retry是專案工程設定。

完整原始查證、方法與版本沿用[Phase 2A](phase-2a-data-sources.md)、各模組Spec與Source Manifest；本次詳細核對記錄在忽略目錄`source-summary.md`。Phase 1網站歷史交付路徑的使用權也需獨立審查，不因TWSE OpenAPI開放而自動取得。

## 6. 效能測量

環境：WSL／Docker Desktop，Docker可見24 logical CPU、15.49GiB RAM；Backend為既有Docker服務，Frontend為本機Production Build，Chromium156。完整DB測試結束並刪除測試DB後才量測API；瀏覽器效能測試等API／SQL量測結束後才開始。沒有模擬實體手機CPU／網路、冷快取或並發負載，沒有壓測正式供應商。

### HTTP與大範圍查詢

同一個HTTP keepalive Client，**每組2次暖機＋20次序列樣本**；下列單位ms，p95採nearest-rank `ceil(.95*n)`。全部200、沒有Timeout。這是本機warm-cache結果，**不是已核准SLO或容量通過標準**。完整raw samples／參數／Response bytes在`api-performance-http.json`。

| 真實資料API／範圍 | 回傳量／限制 | Median ms | p95 ms |
| --- | --- | ---: | ---: |
| 2330一年日K | 241 Bars | 21.30 | 23.38 |
| 2330十年月K | 121 Bars | 92.53 | 134.88 |
| SMA一年顯示區間 | 241 Points，歷史暖機4735 Bars | 277.42 | 312.99 |
| EMA | 同上 | 256.44 | 296.13 |
| RSI | 同上 | 256.07 | 307.69 |
| MACD | 同上 | 255.39 | 299.29 |
| Volume SMA | 同上 | 253.72 | 289.91 |
| 法人一年History | 實際0份，僅測空結果成本 | 2.65 | 3.17 |
| 集保一年History | 實際1份週報 | 8.79 | 9.75 |
| Margin一年History | 實際1份 | 6.46 | 7.74 |
| Lending一年History | 實際1份 | 6.65 | 7.45 |
| 四產品Chip／4日 | 16 Metrics Points | 9.82 | 10.75 |
| 四產品Chip／250日 | 上限1000 Metrics Points，約846KB JSON | 105.19 | 137.89 |
| 全市場data/status | 全庫統計 | **8,347.13** | **8,562.68** |
| chip-data/status（Lending） | 8個來源狀態 | 9.21 | 9.97 |

250日×4的1000點是有界評估／序列化量，不代表已載入1000份完整真實籌碼報表；實際產品Canonical仍稀少。跨來源缺值／未核實狀態也計入輸出量。查詢上限與非法日期等另以31個422案例驗證，不用無限制查詢當Benchmark。

### PostgreSQL自然Query Plan與瓶頸

捕捉實際Application／Public Reader的SQL，在 **RR READ ONLY** Snapshot下對32個不同參數化SELECT執行`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`，每Statement最多15秒。沒有`enable_seqscan=off`、沒有修改Index／資料或強迫Planner。四產品16點的9個SELECT／集保4點的7個SELECT包含身份、來源、觀察與受限資料讀取；這是本次稀疏資料樣本，並非全市場最大量保證。

- 個股一年行情用`pk_daily_bars`及Source PK，主資料讀取約 **0.408ms**，沒有載入其他證券；每日來源與暖機讀取另有有界Index查詢。
- 法人使用`uq_inst_report_natural_key`；Ownership使用`ix_own_weekly_date_instrument`及複合Revision／Item索引；Margin使用`ix_margin_canonical_trade_date`與複合索引；Lending使用`pk_lending_daily_reports`與複合索引。本次0／1筆產品查詢計畫約 **0.042～0.108ms**，只能確認實際計畫／過濾條件，不能據此推估大表。
- **全市場data/status是已確認的效能缺口**：每次讀取都執行整庫Count／Max、按市場COUNT DISTINCT、來源Aggregate與Source-Market UNION。EXPLAIN樣本可見9,712,317列掃描，市場Aggregate約 **6,348.83ms**、來源Aggregate **2,097.39ms**、Source-Market查詢 **3,424.29ms**。市場Aggregate另記錄270,915／271,260個Temporary read／write blocks，各約 **2.07GiB邏輯Temporary I/O**，不是Peak Memory或實體磁碟流量保證。EXPLAIN本身有計時與環境差異，不能將各查詢時間直接加總當HTTP時間。
- 個股／籌碼讀取未發現無界全市場ORM載入；global status本來需要全庫統計，但目前每次全表Aggregate的代價不適合高頻輪詢。12秒Proxy timeout在額外負載下也可能受影響，本次序列量測沒有觸發Timeout。

建議先為全市場狀態定義可接受的更新延遲，再評估有時間戳與完整度語意的摘要／Cache、查詢重用與Index；之後在代表性資料量與並發負載重測。**本次未新增摘要表／Cache，也沒有把建議當成已完成的效能改善。**

### 指標純計算耗時

使用2330已保存 **4,735** 根日K（2007-07-27～2026-11-05），唯讀載入一次，直接呼叫原`DecimalIndicatorEngine.compute`，2次暖機＋20樣本。下列不含DB、HTTP或Pydantic，與上表API時間不能互換。

| 指標 | Wall median ms | Wall p95 ms | 本機Process CPU median ms |
| --- | ---: | ---: | ---: |
| SMA | 5.957 | 30.440 | 5.955 |
| EMA | 5.987 | 28.283 | 5.984 |
| RSI | 11.046 | 36.479 | 11.044 |
| MACD | 10.903 | 35.712 | 10.900 |
| Volume SMA | 6.325 | 31.239 | 6.323 |

### Frontend Bundle與大量圖表

Production Build共24個JS Artifact，合計 **1,210,280 bytes raw／378,028 bytes gzip(level9，逐檔)**，不是單一路由下載量。個股頁本次實際載入9個JS Resource：**226,630 bytes encoded／733,424 bytes decoded**；其中Lightweight Charts動態Chunk為 **183,810 raw／59,356 gzip bytes**。沒有為驗收升級Chart或Next版本。

獨立 **SYNTHETIC rendering-only**負載：1000個日曆日期Candles＋1000個SMA顯示點，以及366個原生籌碼日期×3線＋價格對照。這不是官方交易日、真實覆蓋或指標公式數值驗收；僅該Benchmark攔截本機API，與前述無Mock真實整合測試分開。

| Chart操作 | 桌面1440px median（n=3） | 手機390px（n=1） |
| --- | ---: | ---: |
| 初始91點籌碼Chart ready | 311.11ms | 292.55ms |
| 展開1000 Candles＋1000 SMA | 159.90ms | 141.89ms |
| 展開366點×3籌碼線與價格 | 199.36ms | 199.03ms |

四個Fresh Browser Context的JS Heap單點 **13.55～27.52MiB**；Chromium CDP累積Renderer Task Duration差值 **0.495～0.804秒**、Script **0.187～0.227秒**、Layout **0.056～0.247秒**；DOM快照與原始數據保存於`frontend/render-benchmark.json`。這些不是Peak Memory、整機CPU%、實體手機或長時間Leak測試，n=1／3也不適合聲稱穩定p95。無頁面溢出／Page Error，已檢視截圖。

Chip擴充測量載入366筆後仍保留原重疊視窗；不是宣稱縮放後366筆同時可見。JS Heap未強制GC、Chromium可重用Renderer，快照差異不能直接判定Leak或無Leak。

### Docker Memory／CPU取樣

驗收期間06:59:16（Asia/Taipei）單次`docker stats --no-stream`：Backend **190.6MiB／0.11% CPU**、既有Docker Frontend **111.3MiB／0.00%**、Redis **7.426MiB／0.25%**、PostgreSQL **2.583GiB／102.27%**。Docker CPU百分比可跨核心，102%約一個Logical CPU的使用量；本取樣期間有驗收工作，**不是Idle基準、Peak或容量結論**。Frontend瀏覽器驗收的暫時Node程序不包含於Docker Frontend數字中。

已確認：`pg_stat_statements`未安裝、`shared_preload_libraries`空、`log_min_duration_statement=-1`。因此**歷史慢查詢分布不可取得**；本次以實際有界查詢的EXPLAIN ANALYZE／BUFFERS與HTTP計時補充，不聲稱正式系統沒有慢查詢，也不為驗收變更PostgreSQL全域設定。

## 7. 發現、修正及尚未支援事項

已修正：

- 補上可重複執行的真實Canonical瀏覽器整合案例，分開標示SYNTHETIC UI狀態測試與真實資料，不只依賴Mock驗收。
- 修正Margin／Lending Source Manifest過時Docstring（原先仍描述尚無Adapter），現在正確反映已核准手動Adapter與disabled候選來源；沒有更動公式、Schema或使用權。

未解決／後續前置條件：

- **來源與涵蓋**：TWSE法人、完整TPEx dealer、借券未還、十年籌碼歷史及券商分點維持Blocked／Deferred，不能用Fixture解除。
- **Calendar／時間**：正式交易日曆／週曆及發布SLA不足；缺週不補0、沒有發布時刻不保證PIT。`point_in_time_supported=false`正確保留。
- **跨產品限流**：各模組有自己的PG Host Lock／Limiter，但Institutional／Margin／Lending等鎖Key不同，**尚未共享同一TPEx Host額度**。目前同Host手動工作應依序執行；無人值守並發前需共用Coordinator及跨產品測試，不能宣稱全域5秒限流已完成。
- **公開顯名**：Dashboard已有Source名稱／Code／Revision，但公開發布前仍需審查並補來源、版本、完整授權顯名／OGDL連結。API／Manifest Metadata不等於公開頁面已完成所有顯名要求。
- **Observability／容量**：尚無歷史慢SQL統計、代表性全市場籌碼／長時間並發／長時間記憶體穩定性與正式SLO驗收；本次本機數字不能推估正式容量。
- 既有Starlette TestClient deprecation warning另列依賴維護，不為消除warning進行無關套件升級。

## 8. Phase 3前置條件與重跑方式

進入依賴完整籌碼資料的Phase 3功能前，應先決定其必要資料矩陣，逐項補權限、可重現歷史交付能力、dated identity／單位Evidence、Source Scope／Channel與Ratio分母映射；建立官方交易日／週曆與發布時間政策，補足真實相鄰週／多交易日驗收。之後才可針對實際資料量設定SLO、集中限流、慢SQL監測與容量／長時間穩定性測試。需要公開發布時另完成來源授權顯名審查。

```bash
# Repository根目錄；已安裝專案Python／Node依賴
cd apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/phase-2g2/recheck -- tests -m 'not network' -q --tb=short
cd ../..
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
npm test
npm run typecheck
npm run build
# 對應現有output=standalone，複製本機Build的靜態資產；不部署Docker
mkdir -p apps/web/.next/standalone/apps/web/.next/static
cp -R apps/web/.next/static/. apps/web/.next/standalone/apps/web/.next/static/
# 另開本機Production驗收服務，BACKEND_URL只在伺服器端
HOSTNAME=127.0.0.1 PORT=3100 BACKEND_URL=http://localhost:8000 \
  node apps/web/.next/standalone/apps/web/server.js
# 另一終端；需要Chromium及已保存的真實2330／6488資料
BROWSER_BASE_URL=http://localhost:3100 npm run test:browser --workspace @stockapp/web
docker compose config --quiet
docker compose ps
docker compose exec -T backend alembic current
docker compose exec -T backend alembic heads
docker compose exec -T backend alembic check
```

本次實際Production瀏覽器驗收使用既有`output=standalone`對應的standalone server，而非重新建置Docker。完成本機驗收後以Ctrl-C結束該暫時服務；啟動／停止與數值測量記錄保留在`.tools/validation/phase-2g2/frontend/`。Artifacts與官方Raw副本只存於已忽略的`.tools/validation/phase-2g2/`，不提交或重新公開官方檔案。

主要Evidence：`backend-run.log`、`alembic-{current,heads,check}.log`、`runtime-openapi.json`、`domain-import-audit.json`、`database-{before,after}.json`、`official-archive-comparison.json`、`api-performance-{smoke,http,query-plans,indicator-cpu}.json`、`docker-health-resources.json`、`final-health.json`與`frontend/{vitest,typecheck,build,playwright-full}.log`、`frontend/{bundle,render-benchmark}.json`。桌面／手機及真實資料截圖位於`frontend/screenshots/`。最終四服務仍healthy，`/health`／`/health/ready`／既有首頁皆200。`git diff --check`通過，Next產生的next-env改動已還原，暫時驗收伺服器已停止。

本次交付變更：本報告、README驗收入口、`apps/web/e2e/chip-real.spec.ts`，以及Margin／Lending Source Manifest的說明文字。沒有新產品功能、Migration、環境變數或依賴變更。

本次沒有Commit、Push、公開部署、正式Migration或自動開始Phase 3。既有執行中的Docker前端映像未重建，不能把它的healthy狀態當成新Dashboard已部署。
