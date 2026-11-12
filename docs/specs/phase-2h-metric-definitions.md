# Phase 2H-1 — 券商分點與「主力」代理指標定義

定義日期：**2026-11-13，Asia/Taipei**。狀態：**規格已定義，尚未實作Domain、資料表、Adapter、API或UI**。本次只授權2H-1；來源能力與利用限制見[資料來源查證](phase-2h-data-sources.md)。以下可計算條件不是來源使用許可，也不表示平台已有真實分點資料。

## 1. 名稱與計算邊界

券商分點彙總包含該分點承接的交易，不能識別其個別客戶、資金所有人、受益所有人、投資人規模或單筆委託；同一人也可能透過多個分點交易。總公司／特定分點可能包含經紀與自營成交，只有來源明確拆分才能另行分類。**分點淨買賣超不是分點或客戶的真實庫存變化。**

UI與API必須區分：

- `reported_*`：來源直接提供且通過契約驗證的股數、價格或金額；不能稱為已識別「主力」。
- `derived_*`：本文件明確定義的加總、排名、家數、累計與比例。
- `proxy_*`：研究者選定的分點行為代理值，名稱必須包含方法／參數；不是官方欄位、個別大戶或真正中實戶流量。

不得使用無公式的「主力分數」「主力持股」或把TDCC每週持股變化改稱每日大戶買賣超。正式資料受Blocked條件限制時，回傳Unavailable與原因，不以本文件Synthetic案例生成正式圖表。

**算式可定義不等於有利用或公開展示權。** 後續計算／內用／外用／公開展示／衍生資訊必須各自符合Source Usage Policy與契約；TWSE／TPEx條款對指數或衍生產品等用途另有同意要求時，須先釐清本產品是否涵蓋及取得必要書面許可，不能因只展示Proxy就推論已獲准。權限未知保持blocked，不能由本文件或離線檔案持有狀態自動解除。

## 2. 輸入、相容性與精度契約

### 2.1 一條可比較序列

計算前固定：`instrument_id UUID`、`market_id`、`source_id`、`source_contract_version`、`report_kind`、`trade_scope_version`、`quantity_unit`、`price_basis`、有金額時的`currency`／`price_quantity_basis`，以及`branch_identity_policy/version`。不同市場、來源、原始／更帳後口徑、一般／零股／鉅額／盤後選項不得任意拼接或相加。

`trade_scope`須保存來源實際定義的交易類型／session、是否含零股及鉅額、經紀／自營範圍是否已知、原始成交或更帳修訂基礎。名稱相似不代表互斥；例如「全部」與其「一般」子集合不能同時加總。只有來源契約證明互斥且完整時，才另以版本化合成Scope產生新序列；初版不啟用此合成。

每日來源若按**價格×分點**提供多列，先依核實的價格層級／原始行身分去重，再加總成該分點的同日報表；不同價格列是合法明細，不能誤當重複分點刪除。重播同一份Raw不重複加總；報表修訂選一個被晉升的完整Revision，不把兩版明細混在一起。

### 2.2 券商／分點身分

券商代碼、分點代碼保存為字串，保留前導零及合法字母，不能只用名稱當ID或任意轉整數。查詢歷史日期使用經證據確認的`branch_id UUID`與有效期間Mapping。

跨來源名稱別名、同日不同證券的分點名稱差異，都需正式Crosswalk；不得以模糊名稱、名稱字首或代碼前綴推定總分公司關係。初期市場範圍為TWSE／TPEx；興櫃等其他報表不因欄位名稱相同而混入。

- 已核實單純改名／代碼沿革可在同一Branch UUID下保存Identity History；名稱使用當日有效名稱。
- 代碼重用、合併、停業、承接關係不明時，不自動連成同一分點。初版`identity_policy=as_reported_branch_v1`不把法律或營運上不同的前後分點拼為一條累計。
- 未來若需要「合併承接後經濟群組」，必須另有ID、版本、明確Crosswalk與生效日，不能改寫原始分點報表。

### 2.3 日期與完整度

`trade_date`為交易所本地統計日；發布日／時刻、first_seen、ingested、query_as_of分開。未知發布時刻保持null；觀察到資料不等於已保證歷史Point-in-Time可交易。沿用既有保守As-of Policy，不使用評估時尚未公布／觀察／匯入的Revision。

指定證券與來源Scope下的完整分點排名／家數／集中度／選分點Proxy，需要**完整分點母體或完整活躍分點明細**與完整交付證據。只有TOP表、幾家分點或不完整頁面時，不得宣稱完整TOP15；可另顯示「已取得子集合的觀察值」，但正式排名結果`value=null/status=partial`，不得把觀察子集合當完整母體。`complete`只描述該Source／Scope，不代表已涵蓋其排除的市場參與者或其他交易管道。

期間結果還須確認整個Window的預期日期與實際報表集合。即使每份已載入日報各自完整，自訂區間若缺已確認交易日報表，或交易日／來源交付日曆未核實，也不得宣稱期間完整；不能因日期落在from/to內就自動成為完整期間TOP15。

未出現的分點預設是unknown，不是0。只有契約明定完整明細僅省略無交易分點、整份報表已確認完整、且該分點當日有效，才能據該來源契約判斷`no_activity`；仍須保留此判定來源，不偽造來源曾回報一列0。缺少整個交易日、停牌狀態未知或只載入部分分點時不得套用此例外。

### 2.4 數值

基礎股數／受益或權證單位用精確整數；價格、金額、比例用Decimal。不得用float解析、加總、比較門檻或序列化金融值。沿用既有**獨立Decimal Context precision=50、ROUND_HALF_EVEN、衍生Decimal輸出12位**；來源超精度保留Raw並拒絕不明捨入，超出受支援數值界線也必須拒絕。

股數加總保持整數精確；金額加總與乘法在已核實輸入界線內保持足夠精度，不逐列先捨入再相加。比例僅在最終輸出量化。排名與門檻比較使用未量化的精確值，不能讓顯示捨入改變排名或是否納入。

金融數字輸出字串，例如`"900"`、`"28.205128205128"`；小型排名／分點數／期數可用有界JSON整數。資料未提供或計算受阻為null；真正0保留`"0"`。股數轉張數需該證券／日期／商品經核實的Multiplier；不假設所有商品一張1000股。跨日期Multiplier改變時，不用最後一天係數除整段股數。

## 3. 基本符號與Branch Net Flow（A）

固定一個相容Profile。令`b`為穩定分點UUID、`d`為真正交易日、`W`為查詢日期集合。`B[b,d]`、`S[b,d]`是該分點當日來源口徑的買進、賣出基礎數量：

```text
N[b,d] = B[b,d] - S[b,d]
B[b,W] = sum(B[b,d] for d in W)
S[b,W] = sum(S[b,d] for d in W)
N[b,W] = B[b,W] - S[b,W] = sum(N[b,d] for d in W)
```

`B/S >= 0`，`N`可正、負、0。來源若直接提供Net，必須與精確`B-S`核對；不能為了通過而更改來源值。缺一邊不能當0，只有Net不能憑空拆成Buy／Sell。

| Algorithm ID | Version | 參數／輸出 |
| --- | --- | --- |
| `broker.branch_net_quantity` | `1.0.0` | 分點、trade_date；B／S／N、基礎單位 |
| `broker.branch_range_net_quantity` | `1.0.0` | 分點、from/to及原樣Scope；區間B／S／N、實際session集合與完整度 |
| `broker.branch_net_amount` | `1.0.0` | 僅限已核實精確買／賣金額；`buy_amount-sell_amount`，帶currency |

此處期間量為交易流量總和，不是持股庫存。原始股數在公司行動前後的活動加總，也不自動成為經還原的投資人持倉。

## 4. TOP N分點（B）

Algorithm ID `broker.top_branches`，Version `1.0.0`；參數：`top_n ∈ {5,10,15,30}`、`side=net_buyers|net_sellers`、`from/to`或確認交易日Window、固定Profile、`identity_policy=as_reported_branch_v1`。

**先將每個分點跨整個W彙總，再依N[b,W]排序。**

- 買超榜只含`N>0`：`(-N[b,W], branch_id的UUID bytes升冪)`。
- 賣超榜只含`N<0`：`(N[b,W], branch_id的UUID bytes升冪)`，最負者第一名；顯示Net保留負號。
- 相同淨額用穩定UUID決定順序；分點名稱或可改變的代碼不能作最終Tie Break。結果`rank=1..returned_count`連續、不跳號。
- Net=0不進任一榜。正／負分點不足N時只回實際數量，不用0分點、假名稱或其他交易口徑補滿；完整母體不足N不等於歷史不足。
- 每列同時帶期間Buy、Sell、Net、單位、當日／歷史名稱、Scope及Input Revision，不將Net正值稱為「該分點只有買進」。

區間TOP15不是每日TOP15名單的聯集，也不是先截斷每日TOP15後再相加。若要以交易金額排序，須使用另一個明確`sort_metric`與金額能力驗證；初版不能把股數榜改標成交金額榜。

## 5. 「主力」代理值（C）

這些是可重現的研究Proxy，**不是官方主力買賣超**。UI必須顯示Algorithm名稱、N／Threshold、期間、選擇方式及版本，不只顯示「主力」。

### 5.1 區間TOP N淨額不平衡

Algorithm ID `proxy.broker_top_n_net_imbalance`，Version `1.0.0`；參數`top_n ∈ {5,10,15,30}`，`selection=period_aggregate`固定。

令`P_N(W)`、`M_N(W)`分別為第4節區間買超／賣超榜：

```text
top_positive_net = sum(N[b,W] for b in P_N(W))
top_negative_net_magnitude = -sum(N[b,W] for b in M_N(W))
proxy_net = top_positive_net - top_negative_net_magnitude
```

兩個Magnitude均非負，proxy_net可正、負、0。顯示名稱為「區間TOP N分點淨額不平衡Proxy」。`top_positive_net`不是選中分點的實際買進股數，`top_negative_net_magnitude`也不是實際賣出股數；若需後者，另外回傳選中分點的完整B／S，不偷換欄位標籤。正負選集合互斥，不重複計算Net。

若N已涵蓋全部正負分點、來源又對同一完整成交母體兩側守恆，proxy_net可能正好0；不能因此說「沒有主力交易」。也不能將全分點Net總和接近0視為資料沒有資訊。

### 5.2 每日重選TOP N後累計

Algorithm ID `proxy.broker_daily_top_n_imbalance_sum`，Version `1.0.0`；參數`top_n`同上，`selection=daily_reselect`固定：

```text
daily_proxy[d] = sum(N[b,d] for daily top-N positive branches)
               + sum(N[b,d] for daily top-N negative branches)
period_proxy = sum(daily_proxy[d] for d in W)
```

顯示名稱為「每日重選TOP N分點Proxy累計」。每天成員可以不同，因此不能標成某個固定「主力群組」的持倉或累計；必須保存每日選中Branch IDs及Ranking Inputs。此值與5.1不保證相等，不能用同一Algorithm ID的隱藏預設切換。

### 5.3 高成交額分點日篩選代理值

Algorithm ID `proxy.broker_daily_gross_amount_threshold_net`，Version `1.0.0`；參數`threshold_amount`為非負精確Decimal字串、`currency`必填、`comparison=gte`、`selection=daily_branch_gross_amount`固定。

```text
gross_amount[b,d] = buy_amount[b,d] + sell_amount[b,d]
selected[b,d] = gross_amount[b,d] >= threshold_amount
proxy_quantity[d] = sum(N[b,d] for selected branches)
proxy_quantity[W] = sum(proxy_quantity[d] for d in W)
proxy_amount[d] = sum(buy_amount[b,d]-sell_amount[b,d] for selected branches)
```

輸出名稱為「高成交額分點日篩選Proxy」。它按**分點一天的雙邊成交額**篩選，並不是按個人資產、單筆委託或大單門檻分類，也不是中實戶／大戶。區間先加總金額再篩選是另一種選擇規則，初版不與此ID混用。

只有來源提供精確Side Amount，或完整價格層級資料已核實價格為「該currency／每個Canonical quantity unit」時才可計算：`buy_amount=sum(price_k*buy_qty_k)`、`sell_amount=sum(price_k*sell_qty_k)`。若僅有報表均價、量價近似值、未知金額Scale／幣別／商品報價基礎，回`unsupported/null`；不能用日收盤價、K線VWAP或共同均價乘Net冒充真實成交金額。

供應商的`buy_price`／`sell_price`若是顯示精度有限的均價，也不視為精確Side Amount證據。來源中0、空值或特殊價格須依個別契約處理；例如0表示未提供實際價格時，數量仍可有效，但Amount為unavailable，不能把價格0乘股數當成真實零成交額，更不能把有交易股數改成0。

Threshold=0亦僅選已知精確金額的分點；Unknown Amount不因0門檻而納入。混幣別不能先相加；初版不自動匯率換算。

若任何候選分點的金額未知、無法判斷是否達門檻，完整Proxy為unsupported／null；不能把未知分點默默排除後，仍宣稱取得完整門檻篩選結果。

## 6. 買賣家數（D）

同一分點可同時買進、賣出。以下按**去重後分點UUID**計數，不能數價格層級列數或把一次買進視為一位投資人：

| Algorithm ID（Version皆`1.0.0`） | 定義 | 顯示單位 |
| --- | --- | --- |
| `broker.active_buy_branch_count` | `count(distinct b where B[b,d]>0)` | 分點 |
| `broker.active_sell_branch_count` | `count(distinct b where S[b,d]>0)` | 分點 |
| `broker.active_buy_sell_count_difference` | active_buy_count − active_sell_count | 分點差 |
| `broker.net_buy_branch_count` | `count(distinct b where N[b,d]>0)` | 分點 |
| `broker.net_sell_branch_count` | `count(distinct b where N[b,d]<0)` | 分點 |
| `broker.net_buy_sell_count_difference` | net_buy_count − net_sell_count | 分點差 |

Net=0且B/S皆正的分點同時納入前兩項，不納入淨買／淨賣。B=S=0不納入任何活躍或淨方向計數。缺一邊、身分不明、母體不完整時，完整家數與差值受阻；不能因每列可做減法就聲稱完整。

每日家數相加不是期間「不同分點數」。期間活躍家數另以Algorithm ID `broker.period_active_branch_counts`、Version `1.0.0`計算`count(distinct b where sum B[b,d]>0)`及Sell；期間淨家數則用`broker.period_net_branch_counts`對`N[b,W]`正負計數。API／UI必須標明daily或period，不重用語意不明的`buyer_seller_difference`。

## 7. 集中度、累計、連續與股價對照

### 7.1 分點交易集中度

以`G[b,W]=B[b,W]+S[b,W]`，`G_total=sum G[b,W]`為明確**雙邊**分母。

| Algorithm ID／Version | 數學定義／參數 |
| --- | --- |
| `broker.gross_turnover_branch_share`／`1.0.0` | `100*G[b,W]/G_total`，單位percent |
| `broker.top_n_gross_turnover_share`／`1.0.0` | 先以`(-G,UUID)`排名，`100*sum(top-N G)/G_total`，N同第4節 |
| `broker.gross_turnover_hhi`／`1.0.0` | `sum((G[b,W]/G_total)^2)`，單位fraction，介於0與1；不混用乘10000後尺度 |

分母必須來自同一完整分點母體、同證券／日期／單位／Scope。完整報表可核對`sum B=sum S=T`時，`G_total=2*T`；若使用其他來源交易量T，必須額外核實可比較範圍，不因同日DailyBar存在就套入。部分分點母體的比率不是全市場集中度；分母未知或0時回`unavailable/null`，0/0不回0%。若來源合法覆蓋不含部分交易對手，不強迫總Net=0來修正資料，但也不能宣稱該母體等於全市場。

集中度量測分點承接交易的集中，不是客戶持股集中度、投資人數或主力進出方向；HHI沒有正負買賣方向，不能拿它畫成有號買賣超。

### 7.2 N-session累計

Algorithm ID `broker.branch_session_cumulative_net`，Version `1.0.0`；初版對外預定期間`1/5/10/20/60`，自訂from/to另走區間加總。先用已核實市場日曆取得截至查詢日的最後N個open sessions `W_N`，再計算`sum N[b,d]`。

週末查詢保留最後確認交易日作anchor，不能說週末有新觀察。缺某一個確認session的報表、在查詢範圍內但尚未上市／身分不明、單位／來源Profile改變，回相應missing_data／insufficient_history／incompatible_data與missing_dates；**不把最後N筆觀察當N交易日、不縮短N、不跳過缺日或填0**。

日期區間榜與「N個交易日榜」必須輸出不同的Window Metadata；自訂區間跨休市可合法只有兩個session，但不自行標成5日或20日完整榜。

### 7.3 分點連買／連賣

Algorithm IDs `broker.branch_buy_streak`／`broker.branch_sell_streak`，Version皆`1.0.0`。由最後確認session往回，分別計算連續`N>0`／`N<0`的個數；0與反向值終止。最新值0時，buy_count=sell_count=0。

缺session或Profile／身分改變不能跳過。精確次數受阻時輸出null及已確認下限；掃到已載入歷史左端仍同號且左端未核實，回`truncated_history`，不能把下限當完整次數。合法同UUID改名不打斷，未核實合併／代碼承接不能續接。

### 7.4 Flow vs Price

Algorithm ID `broker.flow_price_alignment`，Version `1.0.0`只定義日期對照，不另發明主力獲利／成本或因果分數。經market_data公開Reader取同Instrument的價格與原本raw／adjusted標記，按真正本地交易日對齊，保留兩邊Input Revision；缺價或缺分點日均留白。

價格與股數／分點差／集中度分開座標與單位；不在前端重算金融公式。未核實還原價或公司行動不推估分點成本，TDCC週資料保持週觀察，不插成每天新資料。共用游標屬後續圖表協調工作，不表示現有獨立Chart已具此功能。

## 8. 中實戶／大戶真實流量（E）與Coverage

Algorithm ID保留為 `investor.medium_large_net_flow`；狀態 **unsupported**，沒有可實作的`1.0.0`金融公式。只有分點彙總無法判斷客戶戶別、單筆交易規模或資產，因此不啟用此指標。即使取得逐筆成交規模，也只能依已核實契約做「成交規模分類Proxy」，不能直接把成交尺寸等同投資人規模；需要另一份來源、分類證據、法律利用範圍與版本化定義。

`broker.source_coverage`／`1.0.0`為解釋性Metadata：已載入報表／已核實session／已知Missing日期、Branch Mapping狀態、完整母體證據、Scope、日期與來源版本、修訂與發布時間可用性。只有存在官方Expected Count／完整交付契約時才算對應coverage fraction，明確列分子與分母；未知總量不以已載入筆數當分母宣稱100%。不產生無校準依據的0–100「主力信心分數」。

## 9. 結果與Input Revision契約草案

後續引擎結果須帶：`metric_id`、`algorithm_version`、全部顯式及預設parameters、公式、instrument／market、window起訖與真正session_dates、quantity_unit／currency／ratio unit、value或null、status／reasons／missing_dates、coverage、是否provisional與PIT限制、query_as_of。

Input References至少有source／contract／scope／parser／promotion版本、Raw／Run／Report Revision、當日Branch Identity Mapping版本、真正統計日、發布／首次觀察／匯入時間；排名／Proxy需另保留所選Branch IDs及排序鍵。不可只留下最後一份Revision或只留下API查詢日期。分點改名或合法Revision晉升後，重算須有新的Reproducibility Key，舊Inputs仍可追溯。

建議沿用既有狀態語意：`complete`、`partial`、`missing_data`、`insufficient_history`、`not_verified`、`incompatible_data`、`truncated_history`、`unsupported`、`unavailable`、`not_yet_available`、`invalid`。完整性受阻的正式衍生`value=null`，合法部分原始觀察與原因可獨立回報；不能用0代表不支援。精確Publication未知時，當前數值可能可算但必須provisional、`point_in_time_supported=false`；嚴格PIT模式保持受阻。

這是H1契約草案，不定死H5正式路由、Pydantic Schema或Schema Migration。後續BrokerFlow Calculator應為唯一分點公式來源，chip_analysis透過公開Port委派；不將分點假裝成InstitutionalCategory，也不改寫InstitutionalCalculator或technical公式。

## 10. 固定數值驗收向量（全部SYNTHETIC）

以下僅供後續Domain數值測試規格與人工驗算。**不是官方分點、真實行情、已授權Fixture、已執行單元測試或正式資料覆蓋。** 分點名稱A等對應固定UUID，其bytes順序按所列代碼升冪；UI改名不得改Tie Break。

### V1：單日完整母體、重疊買賣與0

同一合成市場／證券／已核實Scope，單位shares、全部價格精確20 TWD/share，官方單邊總量1950股；8個分點明示存在：

| Branch | Buy | Sell | Net | Gross quantity |
| --- | ---: | ---: | ---: | ---: |
| A | 1000 | 100 | 900 | 1100 |
| B | 200 | 800 | -600 | 1000 |
| C | 600 | 500 | 100 | 1100 |
| D | 0 | 300 | -300 | 300 |
| E | 100 | 0 | 100 | 100 |
| F | 0 | 200 | -200 | 200 |
| G | 50 | 50 | 0 | 100 |
| H | 0 | 0 | 0 | 0 |

固定預期：

- Buy=Sell=`1950`、全Net=`0`；Active Buy=5、Active Sell=6、Active Difference=**-1**；Net Buyers=3、Net Sellers=3、Net Difference=**0**。這兩種家數差不能互換。
- TOP5／TOP15買超都只有A、C、E，Net順序`900,100,100`，C/E以UUID破同分；賣超為B、D、F，Net `-600,-300,-200`；G/H不補榜。
- TOP5淨額不平衡：Positive=`1100`、Negative Magnitude=`1100`、Proxy=`0`；這不表示沒有成交。
- Gross分母=`3900=2*1950`；A的分點占比=`28.205128205128%`；TOP5 Gross依序A/C（UUID破同分）、B、D、F，合計3700，占比=`94.871794871795%`。
- HHI=`3570000/15210000`，輸出fraction **`0.234714003945`**，不是百分比，也不是乘10000後的分數。
- 每分點精確Gross Amount為`Gross quantity*20`；Threshold=`20000` TWD、比較`>=`時選A/B/C，B恰好等於門檻也選中；Proxy Quantity=`400`股、Proxy Amount=`8000` TWD。

### V2：先彙總再排名不等於每日重選

合成確認session為`d1=2025-12-31`、`d2=2026-01-02`；合成日曆將中間日期設closed，**不宣告真實交易所休市狀態**。13個分點UUID排序：A、D、B1..B5、C1..C6。每格為Net，Buy=max(Net,0)、Sell=max(-Net,0)，0是明示來源值，兩日各Buy=Sell=1500股。

| Branch／組 | d1 Net | d2 Net | W Net |
| --- | ---: | ---: | ---: |
| A | 1000 | -1000 | 0 |
| D | 0 | 1000 | 1000 |
| B1、B2、B3、B4、B5（每家） | 100 | 100 | 200 |
| C1、C2、C3、C4、C5（每家） | -250 | -100 | -350 |
| C6 | -250 | 0 | -250 |

固定預期：

- d1 TOP5正淨額1400、負淨額Magnitude1250，daily_proxy=`150`。
- d2 TOP5正淨額1400、負淨額Magnitude1400，daily_proxy=`0`。
- 每日重選Proxy累計=`150`；區間先彙總TOP5正淨額1800、負淨額Magnitude1750，區間Proxy=**`50`**。不可用150當50，不能先截斷每天榜單再聲稱區間排名。
- A區間Net=0，不進區間任一榜；d2的A sell_streak=1、buy_streak=0，因前一確認session反向而停止。
- 對B1若只載入這兩日，buy lower_bound=2、exact buy_count=null、status=truncated_history；若另有前一確認session的明示Net=0，則精確buy_streak=2。
- 若d1報表缺失，d2不能被當作兩session完整累計；若額外確認一個open session卻缺該日資料，必須missing_data，不靠相鄰日曆天數補0或跳過。跨年不重設累計或Streak。

### V3：價格層級、金額與精度

同一分點兩列：price=`10`、Buy=`50`、Sell=`20`；price=`12`、Buy=`50`、Sell=`30`。預期Buy=100、Sell=50、Net=50股；精確Buy Amount=1100、Sell Amount=560、Net Amount=**540 TWD**。共同簡單均價11乘Net會變550，必須拒絕該替代算式。

額外精度向量：price=`0.10000001`、Buy=`3`、Sell=`1`、計價基礎已核實為TWD/share，Net Amount=`0.200000020000`。Decimal字串門檻`0.40000004`與該列Gross Amount精確相等，`gte`必須選中；不能經float比較而漏選。

### V4：邊界與拒絕矩陣

| 情境 | 固定預期 |
| --- | --- |
| V1將A Buy設null／無法識別特殊值 | 不將其當0；A Net及依賴完整母體的排名／家數／Proxy受阻 |
| 所有B/S明示0且完整母體 | Net與各Count可為0；集中度0/0為unavailable/null |
| 完全沒有報表 | unavailable／空結果，不能回一個全0母體 |
| 相同Raw重播／相同Canonical Revision重複輸入 | 不重複加總；引擎輸入重複natural key須拒絕或由Reader去重並可證明同一Revision |
| 同分點相同價格層級列重複／不同價格列 | 前者按契約原始行身分拒絕重複；後者合法彙總，不誤刪 |
| 兩個市場同樣分點代碼，或一個代碼被重用 | 不以代碼直接合併；依市場／日期Mapping，不明者unresolved |
| 同日一般報表與已含一般的全部報表 | incompatible Scope，不相加 |
| 原單位張但Multiplier未知／不同日期係數 | unsupported單位轉換；不能默乘1000或套最後一天係數 |
| 只有均價、混幣別或未知金額Scale | Amount Threshold Proxy unsupported/null；數量Net能力不因此變成金額能力 |
| 來源價格0為已定義的未知價格Sentinel | 股數保留原值，金額unavailable；不產生零成交額或偷偷跨入其他市場 |
| 數量負值、NaN／Infinity、無效日期、超界值 | invalid，保留Raw／Audit，不自動修正 |
| TOP N為0、4、16、bool／float | 非本版允許參數，後續API應422；不得靜默改成15 |
| 舊Revision在新Revision後重播 | Canonical不能倒退；重新計算仍指向政策允許的Revision |
| Publication／Observation晚於query_as_of | not_yet_available/null；未知Publication不宣稱PIT |
| 只取得一頁TOP15名單 | 可揭示來源觀察表，但不能聲稱全市場家數／集中度／完整期間TOP15 |
| 任一分點未核實身分或一個confirmed session缺報表 | 完整排名／N-session結果不得標complete；附missing_dates／identity原因 |

## 11. 後續授權子階段的驗收輸入

本文件只提供公式與固定預期值；H2–H7尚未執行，也不建立本文件指標的正式測試或Migration。後續另行授權時：

1. H2先確認Source Contract、分點Identity History與互斥Scope，再設計版本化完整報表／Raw／Audit／Revision／Canonical；不改Phase 1–2資料規則。
2. H3按已核准交付契約驗證離線Fixture、編碼、欄位／日期／單位、ZIP安全與原子報表，不執行EXE；沒有正式權限的下載保持disabled。
3. H4將V1–V4變成固定數值測試，新增唯一BrokerFlow Calculator及公開Reader／Calculation Ports；重用既有精度、Calendar／Availability與Lineage概念，不能複製法人公式或在前端算Proxy。
4. H5／H6先確認正式Contract再擴充同源Typed Client與既有Chart，清楚命名Proxy、來源、範圍與單位；同步游標／副圖協調另作測試，不建立第二個股票平台。

目前真實券商分點資料覆蓋為 **未取得已核可報表／Blocked**；上述數學可定義能力不解除來源使用權、外部展示權、完整母體、已核實交易日曆或歷史涵蓋的前置條件。
