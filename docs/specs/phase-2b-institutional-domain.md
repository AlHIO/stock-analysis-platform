# Phase 2B-1 — Institutional Trading Domain & Contracts

日期：2026-11-07，Asia/Taipei。依本次任務，只實作純 Domain、必要 Protocol 與固定數值測試。沒有供應商 Adapter、ORM、Migration、API、CLI、Redis、排程或正式資料匯入；未操作 Docker／歷史回補 Worker，也不執行資料庫整合測試。

依據：[2A來源查證](phase-2a-data-sources.md)、[Phase2領域設計](phase-2-domain-model.md)、[API草案](phase-2-api-contract.md)、[roadmap](phase-2-roadmap.md)。本文件是本次子階段的實作契約，不代表上述未實作的API已存在。

## 1. 已先揭露的規格差異與解法

| 差異／尚未定義處                                               | 本次決策                                                                                                                  |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 原roadmap的2B-1包含audit tables／Migration，本次明確禁止DB操作 | 本次以最新任務為準，只實作institutional/domain；儲存與匯入延後2B-2或另行授權子任務，不建立任何資料表                      |
| 2A把籌碼公式放未來chip_analysis，本次要求institutional計算服務 | 法人公式只在InstitutionalCalculator實作一次；未來chip_analysis經公開InstitutionalCalculationEngine Port委派，不複製公式   |
| 2A數量為int/BIGINT，本次要求Decimal精度                        | 來源股／單位數與買賣超維持整數；衍生計算結果為Decimal，採私有50位Context；拒絕float、bool、隱式張股換算。不是允許零碎股數 |
| 既有行情日期不能證明完整交易日曆                               | 輸入CalendarSnapshot需明示範圍、已確認開市session、verified及版本；不從週一至五或有資料的日期猜日曆                       |
| 連續次數遇輸入左界仍同方向，原規格未定義                       | 回報truncated_history及已確認下界，不假裝那就是完整連續次數；只有呼叫端核實該證券歷史起點才可視為真正起點                 |

Phase2A未確認的T86／TPEx網站使用權、2016舊外資分類生效界線仍維持未確認。本次只用明示synthetic資料，不聯網補查、不啟動來源。

## 2. 檔案與模組邊界

```text
apps/api/src/stockapp/modules/institutional/
  __init__.py
  domain/
    __init__.py
    models.py          immutable值物件、報表、來源／時間／scope、calendar/history
    results.py         計算狀態、輸入lineage與結果
    calculations.py    唯一法人日／累計／連續計算實作
    ports.py           公開reader、repository與calculation Protocol
apps/api/tests/
  test_institutional_models.py
  test_institutional_calculations.py
```

Domain僅import標準庫及自身檔案，不import FastAPI、SQLAlchemy、Redis、httpx、供應商SDK或其他模組私有Infrastructure。不改market_data／technical的程式碼。模組import不讀.env、不建立engine、不發請求。

## 3. 資料契約

### 3.1 InvestorFlow與法人分類

來源欄位：`category`、`buy_quantity`、`sell_quantity`、`net_quantity`、`mapping_status`。quantity只接受嚴格int或None；買賣非負BIGINT，net允許signed BIGINT。float、bool、Decimal（即使看似整數）、數字字串、NaN、Infinity不可直接當來源數量；Adapter未來須明確解析並驗證，不能靜默截位。

- `resolved_net`：來源有net則保留；沒有net但buy/sell均已知時精確計算buy−sell。原net欄仍保持None，方便區分原始未提供與可確定的推導。
- 三值皆有時嚴格要求net=buy−sell；不相等拋InstitutionalValidationError，不改寫來源net。
- 只有net的來源total合法，buy/sell仍為None，不反推缺少細項。
- `foreign_unspecified`只能是mapping_status=unverified，不能直接標現代foreign_ex_dealers。

| category            | aggregation role        | 合計規則                                            |
| ------------------- | ----------------------- | --------------------------------------------------- |
| foreign_ex_dealers  | leaf                    | 三大法人之一，不含外資自營商                        |
| investment_trust    | leaf                    | 投信                                                |
| dealer_proprietary  | leaf                    | 自營商自行買賣                                      |
| dealer_hedging      | leaf                    | 自營商避險                                          |
| dealer_total        | subtotal                | 只取self+hedge一次                                  |
| three_major_total   | subtotal                | 只取foreign_ex+trust+self+hedge一次                 |
| foreign_dealers     | supplemental            | 不再加進三大合計                                    |
| foreign_inclusive   | supplemental            | 與foreign_ex／foreign_dealers重疊，不再加進三大合計 |
| foreign_unspecified | supplemental/unverified | 歷史分類未核實，不能代入現代公式                    |

已知且mapping_verified的children與來源subtotal會逐欄驗證buy、sell、net，包含foreign_inclusive；即使self/hedge缺失，已知foreign_ex＋trust＋dealer_total與來源three_major_total仍須對帳。但這僅驗證來源assertion，不是用subtotal填補衍生計算的缺項。資料不足時不聲稱已完成對帳。一次報表只能每category一筆；缺失可以用沒有component或欄位None表示，都不能自動補0。

### 3.2 InstitutionalDailyReport

一份instrument UUID／market UUID／本地trade_date／quantity_unit／TradeScope／taxonomy_version，帶不可變components、FactLineage與SourceTiming。列表輸入會複製成tuple，呼叫端之後更動列表不改變快照。

`quantity_unit=shares|units`，不接受lots。`TradeScope`包含來源定義的code（含universe）、是否含鉅額／零股／盤後、原始成交或corrected basis；任一未知即不能認為scope已核實。相同字面code但flags不同仍是不同口徑。

報表完整度由必要四個leaf的buy/sell/net可取得性及mapping／scope判斷：

- complete：四個leaf均可確認buy/sell與net，scope與分類已核實。
- partial：缺少必要欄位／category，即使來源自己的schema可完整解析，也不是完整法人明細。
- not_verified：scope未知、必要分類未核實或包含legacy foreign_unspecified。

**TPEx I2的20-key來源情境**：可保留外資／投信、dealer_total與three_major_total的來源值；缺self/hedge時整份report仍partial。計算衍生dealer／three_total時回缺少component，不拿reported subtotal反推或掩蓋缺欄。

單獨計算已確認的外資欄可能成功；其結果仍附原報表partial狀態。計算的complete只指指定欄位／指定窗口的可確認性，不能拿來改寫整份report完整度。

### 3.3 時間與lineage

- `trade_date`是統計所屬本地DATE，拒絕datetime冒充交易日。
- `source_published_on`與`source_published_at`皆可為None；只知日期不補午夜。有兩者時按IANA來源時區核對一致。
- `first_seen_at/last_seen_at/ingested_at`為aware datetime，驗證順序；不把下載或寫入時間當成發布時間。
- FactLineage保留source UUID、source_contract_version、parser_version、revision/run/raw UUID、原始row locator、normalized payload_hash、promotion_policy_version。此階段未套用儲存promotion政策時標`not_applied`，不宣稱已選出正式canonical。
- 計算結果保留輸入report的完整lineage／timing／scope／taxonomy／report completeness，以及algorithm與calendar版本。

`as_of`是計算的統計日期截界，不是資料在歷史當時已公開的時間截界。當前revision可能在較後發布／觀察；本階段不宣稱提供point-in-time回測或公開時間過濾。

## 4. CalendarSnapshot與InstitutionalHistory

CalendarSnapshot：market UUID、covered start/end、嚴格遞增不重複session_dates、verified、version。verified是呼叫端已核實該範圍完整session清單的聲明，不是Domain自行查證。半日市同樣是一個session；週末可有補行交易，不能用weekday替代。

InstitutionalHistory：單一instrument／market、calendar、唯一且遞增的報表、history_start_verified。報表日期必須在明列session內；重複日期即拒絕，包含同日不同source／revision，不能用last-write-wins選其中一筆。未來Repository需先依明確政策選revision。

`history_start_verified=True`必須指已核實的該證券歷史起點，不能因「資料庫最早只有這筆」就設定；其calendar範圍也要以這個起點為界。沒有此證據維持False。

時間序列的相容key是source_id、source_contract_version、taxonomy_version、完整TradeScope與quantity_unit；instrument／market先由History驗證。即使數字相同，不同來源或契約也不自動合併。parser/revision可以不同但要保留lineage；將來若要跨來源相容政策，必須另有可審查的equivalence contract。

## 5. 公式與初始化

所有衍生數量為Decimal，算法版本`1.0.0`；不使用float。來源int做差不受Decimal環境影響；衍生加總用獨立precision=50 Context，不繼承呼叫端precision／rounding／traps。只有整數數量相加，不做價格調整或比例估算，也不需要捨入來源股數。

### 5.1 日淨額與分類合計

```text
net = buy_quantity − sell_quantity（或已核對的reported net）
dealer_total_net = dealer_proprietary_net + dealer_hedging_net
three_major_total_net = foreign_ex_dealers_net + investment_trust_net
                      + dealer_proprietary_net + dealer_hedging_net
```

`daily_net`遇dealer_total／three_major_total也走上述disjoint leaf公式，不改用來源subtotal作缺項fallback。未確認mapping或scope回not_verified；缺少必要leaf／net回missing_data。所有加數真為0時，結果才是Decimal(0)。

### 5.2 最近N交易日累計

`cumulative_net`支援N=5、10、20、60；`cumulative_windows`一次回四個標準窗口。未知period、bool或其他型別拋驗證錯誤。Phase2A未來泛用API的1–250草案不代表本次已開放任意窗口。

以已核實calendar中`session_date <= as_of`的最後一日作anchor，再取最後N個預期session：

```text
cumulative_net(N, anchor) = Σ daily_net(session)；session為最後N個確認交易日
```

- as_of落在covered範圍的週末／假日，anchor可為前一個確認session；不創建當日報表。
- 少於N個session為insufficient_history；已知session缺報表／目標欄為missing_data，value=None。
- 不取最後N筆「有資料」的紀錄來跳過缺日；不提供看似完整的partial sum。
- 日曆未核實或查詢超出covered range回not_verified；沒有session回no_sessions。
- 指定窗口中來源、契約、分類、scope或單位不同回incompatible_data；不把shares與units相加，也不自動張股換算。
- as_of之後的輸入不參與。每日值受BIGINT限制，但60日總和可以超過BIGINT，仍以精確Decimal回傳，不錯套每日上限。

### 5.3 連續買超／賣超

從anchor往前掃已確認、相鄰session的目標net。net>0為買超、net<0為賣超、net=0中斷兩者。

- 最近一天為0，buy_count=sell_count=0，這不是缺資料。
- 方向相反的已確認前一session中止該streak；目前買超時連賣為0，反之亦然。
- 同方向回溯遇缺報表／缺欄／未知分類／不相容口徑時，該方向完整count=None；保留已確認的lower_bound，不能跨過缺口繼續算。
- 到輸入左界仍同向且無history_start_verified證據：truncated_history、left_censored=True，只能確認下界。若起點確實核實，則可回完整count。
- 日曆未知時不能把觀察序列當成連續交易日，也不宣稱已確認多日連續次數。

結果的formula、reason、missing dates/components、session dates、input refs與calendar_version可解釋為何是完整值、缺失或下界。missing_dates指「目標net不可取得」的session，包含缺整份report或缺必要欄，不表示該市場休市／整份行情都缺失。streak的session_dates/input_refs可包含已知0／反向的終止邊界作為證據，但該邊界不計入連續次數。即使空結果也保留instrument／market UUID。此服務不產生交易訊號或識別個人／券商主力。

## 6. Domain Ports

- `InstitutionalHistoryReader.load(...)`：由未來Adapter取得單一證券、來源／contract／scope／unit的typed history，附calendar證據；此階段沒有實作。
- `TradingSessionReader.load(...)`：明示日曆範圍與verified狀態，不讀行情缺日來猜休市。
- `InstitutionalRepository.get/upsert(...)`：未來持久化邊界，不隱式commit；整份報表、revision與lineage必須同交易處理。本次只有Protocol，沒有SQL或連線。
- `InstitutionalCalculationEngine`：daily_net、dealer_total、three_major_total、cumulative_net、cumulative_windows、streak，未來API／chip_analysis共用。

## 7. 固定數值驗證與操作

測試檔案明確標記synthetic；只在記憶體建模型，UUID／來源名稱不代表正式資料。必要行為包括買賣差值、dealer hierarchy、foreign supplemental排除、缺欄、缺session、週末／跨年／補行交易、5/10/20/60數值、0／正負streak、左界截斷、重複日期、單位／來源口徑差異、BIGINT邊界與低precision/traps環境。

從repository根目錄執行，不需要Docker／PostgreSQL／Redis：

```bash
.tools/api-venv/bin/python -m pytest apps/api/tests/test_institutional_models.py apps/api/tests/test_institutional_calculations.py -q
RUN_INTEGRATION=0 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration' -q
```

Domain isolation test會以只有標準庫的`python -S`匯入模型／Port／計算器，另以AST檢查dependency boundary；不啟動FastAPI app或讀取.env。

## 8. Phase 2B-2 待辦（本次未開始）

1. 核實啟用來源的自動利用／保存／對外服務權與歷史分類契約；TPEx I2缺欄仍不可冒充完整明細。
2. 以本次typed契約規劃Adapter／Normalizer與明確來源capabilities；網路timeout/retry/host協調留Infrastructure。
3. 另行審查Schema／audit／revision／promotion policy與Migration，建立公開InstrumentIdentityReader與storage實作；不能在本次純Domain驗證中順帶建立表。
4. 接入可核實calendar及證券歷史起點，確認Report／History來源一致性；不能用Phase1已存行情當完整session名單。
5. 加入raw-first服務與有隔離schema的PG整合測試；API／Pydantic／OpenAPI與CLI依後續授權範圍執行。所有新功能共用本次calculator，不另抄公式。

本次完成後停止，不自動進入2B-2。

## 9. 本次實際測試與操作邊界

| 檢查                        | 結果                                                                                                                                                  |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Institutional獨立unit tests | **140 passed**：models 90、calculations 50；包含`python -I -S`無site-packages獨立import與AST依賴檢查                                                  |
| 全部後端非整合回歸          | **852 passed、2 skipped、206 deselected**；2 skipped為明確停用的官方network smoke，206為排除的integration；1項既有Starlette/httpx deprecation warning |
| 既有前端Vitest              | **244 passed，15 files**                                                                                                                              |
| Typecheck／Production Build | 本機指令通過；未部署，build產生的next-env恢復原內容，前端程式無diff                                                                                   |
| 現有OpenAPI snapshot        | export `--check`通過；沒有新route或OpenAPI變更                                                                                                        |
| Git／文件                   | diff whitespace、Markdown格式與相對連結檢查通過；existing market_data/technical/cli/worker/Compose/migrations保持無diff                               |

另外執行的既有檢查：

```bash
PATH="$PWD/.tools/node/bin:$PATH" npm test
PATH="$PWD/.tools/node/bin:$PATH" npm run typecheck
PATH="$PWD/.tools/node/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 npm run build
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
git diff --check
```

沒有執行Docker指令、Alembic、PostgreSQL／Redis整合測試或真實供應商network tests，也未查詢／寫入正式資料庫、停止或重啟Container、修改Ingestion Worker、改動歷史行情、commit／push／部署。沒有以未執行項目宣稱通過。正式來源／schema／API工作留待下一個明確任務。
