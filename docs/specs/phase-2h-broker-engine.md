# Phase 2H-4 — 券商分點分析引擎

驗證日期：2026-11-18，Asia/Taipei。範圍為純 Domain 計算、公開 Ports、固定數值測試與文件。沿用 [H1 指標定義](phase-2h-metric-definitions.md)、[H2 Domain／Persistence](phase-2h-broker-persistence.md)及 [H3 離線匯入](phase-2h-broker-ingestion.md)，沒有建立 API、UI、資料表、Migration、排程或下載服務。

## 1. 交付與架構

公開入口為 `stockapp.modules.broker_flow.public`：

- `BrokerFlowEngine.calculate(request, history)`；`BrokerFlowCalculator` 是同一實作的別名。
- `BrokerMetricRequest`、`BrokerMetricResult`、`BrokerAnalysisHistory`。
- `CanonicalBrokerInput`、`CalendarSnapshot`、`PriceObservation`、`BrokerInputReference`。
- `BrokerAnalysisReader`、`BrokerPriceReader`、`BrokerCalculationEngine` Protocols。
- 唯讀 `BROKER_METRICS` 登錄表、`MetricStatus`、`AvailabilityPolicy`。

`domain/engine.py` 統一驗證參數、固定預設值並分派計算。`analytics.py` 定義不可變輸入／輸出；`analytics_context.py` 負責日曆、來源、身分及 As-of Gate。`flow_calculations.py` 實作共用分點彙總、排序、家數、累計及連續；`proxy_calculations.py` 重用該彙總，實作集中度、Proxy、Coverage 及股價對照。

Domain 不匯入 FastAPI、SQLAlchemy、Redis、HTTP 或供應商 SDK；公開入口匯入隔離有獨立程序測試。引擎不查 Raw、不挑選資料庫 Revision、不執行 I/O。Reader Protocol 的 PostgreSQL 組裝及 API 留待後續階段；本次未宣稱已完成 Reader 的資料庫實作。既有 InstitutionalCalculator、Technical Engine 與 Ingestion Worker 均未修改。

串接形式如下，呼叫端提供 Request 與已核實的 Canonical Snapshot；沒有從 Fixture 產生正式數據：

```python
from stockapp.modules.broker_flow.public import (
    BrokerAnalysisReader, BrokerFlowEngine, BrokerMetricRequest,
)


def analyze(request: BrokerMetricRequest, reader: BrokerAnalysisReader) -> dict:
    # 後續 Application 應在同一個唯讀 transaction snapshot 內載入所需輸入。
    history = reader.load(request)
    return BrokerFlowEngine().calculate(request, history).to_dict()
```

## 2. 輸入、相容性與時間

`BrokerMetricRequest` 必填 instrument／market UUID、metric ID、`start/end` 本地日期與帶時區的 `query_as_of`。日期不可反向、超過 3,660 日差或晚於 As-of 的市場本地日期。`timezone_name` 預設 `Asia/Taipei`。不支援的指標 ID、未知參數、float、無效 UUID、非法 N 或混淆的選擇模式會拋出 `BrokerFlowError`。

`CanonicalBrokerInput` 包含既有 `BrokerTradingDailyReport`、Observation UUID、Promotion Policy Version、當日身分映射及明確的 `canonical_verified`；預設不信任為已核實。完整度與 `branch_universe_complete` 都必須成立。相同自然鍵的多份 Revision 不會被引擎任選或相加，輸入直接拒絕重複；由 Reader 選出合法 Canonical。

比較 Profile 包含 Instrument、Market／Timezone、Source／Namespace、Contract／Schema／Parser、Report Kind／Trade Scope、單位、價格／金額基礎、幣別、身分 Policy 及 Promotion Policy。不同 Profile 不能拼接或加總。同一日多個 Scope／Source 也不會被自動視為互斥。

每筆分點 Trade 必須對上該來源、Namespace、市場與交易日有效的 verified Identity ID／Branch UUID。沿用 `BrokerIdentityBook` 拒絕重疊的已核實代碼或分點映射；未知代碼、名稱相似或合併承接不能推定為同一分點。合法改名保存原 UUID 與當日映射，不影響 UUID Tie Break。

### 2.1 交易日與缺失

`CalendarSnapshot` 明列 `[start,end]` 覆蓋範圍、Open Dates、版本、證據、時區及觀察時間。只有 verified 且截至 `query_as_of` 已知的日曆可建立預期交易日分母。範圍內未列日期才可判為休市；不自行按週一至週五推算。

- 單日基本計算可以使用可信日報，不必憑空建立日曆；Coverage 分母仍須核實日曆。
- 多日區間必須覆蓋整個日期範圍，不能用已載入日期推定完整。
- N-session 從日曆取截至 `end` 的最後 N 個 Open Sessions，可早於 Request.start；結果回傳實際 Window，不取最後 N 筆觀察代替。
- Streak 從最後確認交易日往回，需要讀到終止值才有精確次數；週末不創造新觀察。
- 核實休市日卻有來源報表為 `invalid`。缺交易日、缺分點觀察都不能跳過。

預設缺分點是 Unknown。唯一的 `no_activity` 例外須同時提供完整 Canonical、當日有效身分、來源省略無交易分點的 Policy Version 及 Evidence Reference，且該分點沒有來源 Trade 列；這只是可追溯的衍生零值，不偽造來源曾回報零列，也不能填補缺少整份報表的日期。

### 2.2 As-of 與發布時間

預設 `observed_revision_v1` 要求已知發布時刻、首次觀察及匯入時間均不晚於 `query_as_of`；發布時刻未知時，當前已觀察數值可以計算，但必須 `provisional=true` 且 `point_in_time_supported=false`。

`published_and_observed_revision_v1` 額外要求發布時刻已知，否則阻擋計算。兩種 Policy 都不以執行日取代真正交易日，不採用未來才觀察／匯入的修訂。

PIT 還需要 Reader 明確提供 `point_in_time_verified` 及對應的歷史版本選擇證據；預設 false。只有所有被引用輸入已可取得且 Canonical／身分證據成立時才可能為 true。引擎不憑最新 Canonical、下載時間或宣告文字自行發明歷史版本保證。保留被拒絕候選的診斷不代表採用其金融值。

## 3. 指標登錄與公式

Engine Version：`broker-flow-engine-1.0.0`。下列可實作算法皆為 `1.0.0`，完整公式文字隨結果回傳。`investor.medium_large_net_flow` 保留 Unsupported，沒有虛構公式或算法版本。

固定同一相容 Profile，令 `B[b,d]`、`S[b,d]` 為分點 b、交易日 d 的買進與賣出基礎數量，`N=B-S`；區間 `W` 的 B／S／N 分別加總。`G=B+S`，`G_total=sum(G)`。數量單位沿用來源 Canonical，不自動除以 1,000 換成張。

| Metric ID | 公式／行為 | 參數 |
| --- | --- | --- |
| `broker.branch_net_quantity` | 每日每分點 B、S、B−S | `branch_id` 可省略以回傳全部已報分點 |
| `broker.branch_net_amount` | 每日精確 Buy Amount−Sell Amount | `branch_id` 可省略；需已核實 Side Amount／幣別 |
| `broker.branch_range_net_quantity` | 指定分點區間 B、S、N 加總 | `branch_id` 必填 |
| `broker.top_branches` | 先彙總每分點整個 W，再排正／負 Net | `top_n=15`；`side=net_buyers` 或 `net_sellers` |
| `broker.branch_session_cumulative_net` | 最後 N 個確認 Open Sessions 的分點 Net 總和 | `branch_id`；`sessions=5`，允許 1/5/10/20/60 |
| `broker.branch_buy_streak` | 往回連續 N>0；0 或反向停止 | `branch_id` |
| `broker.branch_sell_streak` | 往回連續 N<0；0 或反向停止 | `branch_id` |
| `broker.active_buy_branch_count` | 每日 count(B>0) | 無 |
| `broker.active_sell_branch_count` | 每日 count(S>0) | 無 |
| `broker.active_buy_sell_count_difference` | 每日 active_buy−active_sell | 無 |
| `broker.net_buy_branch_count` | 每日 count(N>0) | 無 |
| `broker.net_sell_branch_count` | 每日 count(N<0) | 無 |
| `broker.net_buy_sell_count_difference` | 每日 net_buy−net_sell | 無 |
| `broker.period_active_branch_counts` | 區間 sum(B)>0／sum(S)>0 的不同分點家數 | 無；不是每日家數相加 |
| `broker.period_net_branch_counts` | 區間 N[b,W] 正／負家數 | 無 |
| `broker.gross_turnover_branch_share` | 100×G[b,W]/G_total，percent | `branch_id` |
| `broker.top_n_gross_turnover_share` | 100×sum(TOP N gross)/G_total，percent | `top_n=15` |
| `broker.gross_turnover_hhi` | sum((G[b,W]/G_total)^2)，fraction | 無；不乘 10,000 |
| `proxy.broker_top_n_net_imbalance` | 區間 TOP N 正 Net 加上 TOP N 負 Net | `top_n=15`；`selection=period_aggregate` |
| `proxy.broker_daily_top_n_imbalance_sum` | 每天重選 TOP N 正負分點，再加總 Daily Proxy | `top_n=15`；`selection=daily_reselect` |
| `proxy.broker_daily_gross_amount_threshold_net` | 每日依 Buy Amount+Sell Amount≥門檻選分點，累計 Net Quantity／Amount | 必填 `threshold_amount` 精確 Decimal 字串與 `currency`；固定 `comparison=gte`、`selection=daily_branch_gross_amount` |
| `broker.flow_price_alignment` | 真正本地交易日 Full Outer 日期對照 | 見下節 |
| `broker.source_coverage` | 已知日曆、報表、母體、Mapping、版本及缺失 Metadata | 無；沒有任意信心分數 |
| `investor.medium_large_net_flow` | Unsupported；分點彙總不足以識別投資人規模 | 無 |

所有 TOP N 只允許 5/10/15/30。買榜依 `(-N, UUID.bytes)`，賣榜依 `(N, UUID.bytes)`，Gross 依 `(-G, UUID.bytes)`。零 Net 不進正負榜；不足 N 不補人；Rank 連續。每日 TOP N 累計與區間先彙總的選擇規則不可互換。

同一分點 B>0 且 S>0 可以同時算在 Active 兩邊；Net=0 不算在淨買／淨賣家數。來源直接提供的 `reported_net_quantity` 與 derived Net 分開；期間的來源 Net 加總明稱 `sum_reported_daily_net_quantity`，不能冒充來源原始期間欄位。

分母必須是同一完整來源母體的雙邊數量。未知或零分母為 Unavailable，不回 0%。門檻 Proxy 必須知道全部候選的精確 Side Amount；未知金額不能因門檻為零而被當成已知，也不以共同均價、收盤價或近似 VWAP 乘 Net。未核實的單位／幣別不換算。

### 3.1 Flow 與股價對照

`broker.flow_price_alignment` 參數：

- 分點模式：`flow_metric=broker.branch_net_quantity`（預設）、必填 `branch_id`、`flow_parameters={}`。
- Proxy 模式：`flow_metric=proxy.broker_daily_top_n_imbalance_sum`、不可指定 Branch、`flow_parameters={"top_n":15,"selection":"daily_reselect"}`。每日曲線重用既有 Daily Proxy，不把期間 Proxy 畫成每日數值。
- `price_basis=raw`（預設）或 `adjusted`；選擇單一 Price Profile，不暗中拼接來源、幣別或還原基礎。

每點保留原始日期、Flow／Price 各自單位、來源與 Revision；Proxy 額外保留當日選中 Branch IDs、參數、公式及版本。實際日期採 Full Outer 對照；缺任一邊為 null，完整結果受阻時金融 `value=null`，可在診斷觀察中看見可採用的日期與缺失原因。尚未可取得的價格只顯示日期／拒絕原因，不洩漏其價格值。

不做 Forward Fill、插值、因果分數、主力成本或庫存推算。股價與股數為不同量綱；後續圖表須分軸。此處不建立週資料，也不將 TDCC 週觀察日化。

## 4. 精度與結果契約

數量加減維持 Python int 精確值；金額與比例使用獨立 Decimal Context，`precision=50`、`ROUND_HALF_EVEN`。精確金額彙總不可暗中丟失有效位；既有 Domain 的價格／金額輸入界線維持 NUMERIC(38,12) 語意。拒絕 float、NaN、Infinity、超出界線／過深的 Metadata 或未知參數。

只有衍生 Decimal 顯示值在 `to_dict()` 最終量化至 12 位；參數、來源 Metadata 與 Reproducibility Hash 保留未量化精確值，因此顯示捨入不改變門檻、排序或輸入身分。金融整數／Decimal 輸出字串，小型家數、Rank、期數保持整數。Null 與真正的 `"0"` 不混用。

結果包含：

- 原始 Request、所有明示／預設參數、公式、Metric／Algorithm／Engine Version、單位。
- `value`、`status`、`reasons`、真正 `expected_dates`、`missing_dates`、Window。
- `input_refs`：Raw／Run／Revision／Observation UUID、Payload SHA256、Source／Contract／Schema／Parser／Promotion／Identity Policy、Scope、單位、統計日期與發布／首次觀察／匯入時間。
- 當日 Identity 的代碼、名稱、有效期間、來源／市場、事件、證據；Calendar Version／Coverage／Evidence；Canonical Verification 與 Snapshot Fingerprint。
- `selected_branch_ids`、`provisional`、PIT 證據與 `point_in_time_supported`。
- `reproducibility_key`：SHA256，包含完整 Request、輸入參照、證據與結果；修訂、Mapping、日曆、參數或算法改變會改變此 Key。

`complete` 是指定來源／Scope 的計算完整，不代表全市場所有交易或十年完整歷史。任何非 Complete 的正式金融 Value 都是 null；Partial 原始觀察與覆蓋資訊另留於 Details。

主要狀態包括 `partial`、`missing_data`、`not_verified`、`insufficient_history`、`incompatible_data`、`truncated_history`、`no_sessions`、`unsupported`、`unavailable`、`invalid`、`not_yet_available`。Streak 掃到載入左端仍同向時，精確值為 null，Details 只提供確認下限；不將下限當成精確次數。

Coverage 按核實交易日分母與完整交付證據回報日期覆蓋，另外區分 Partial／Unverified 報表；不能用已載入報表自己充當完整分母。沒有建立未經校準的信心分數。

## 5. 固定數值測試

四個新增測試檔全部使用明確標記的 SYNTHETIC 輸入；以下不是官方資料覆蓋：

| 向量／邊界 | 固定預期 |
| --- | --- |
| H1 V1 買／賣總數 | 1950／1950，Net=0 |
| V1 Active 家數差／Net 家數差 | −1／0，兩種定義不混用 |
| V1 TOP 買榜／賣榜 | Net 900,100,100／−600,−300,−200；同分依 UUID |
| V1 Gross 分母、A share、TOP5 share | 3900、28.205128205128%、94.871794871795% |
| V1 HHI | 0.234714003945 fraction |
| V1 金額門檻 20000 TWD（含等號） | Proxy Quantity=400，Amount=8000 |
| H1 V2 跨年兩日 Proxy | 每日重選 150+0=150；期間先彙總=50 |
| V2 Flow／Price 對照 | 每日 150／0，保留原交易日及雙方 Revision |
| V2 買賣方向反轉 | A 最新 Sell Streak=1、Buy Streak=0 |
| 左端未確認、缺日、非交易日 | truncated_history 下限／missing_data／invalid，不補零 |
| 精確小數門檻 | 0.40000004 gte 可精確選入；Net Amount=0.200000020000 |
| 真正零、空、部分、缺金額 | 保留零／Unavailable／Partial／Unsupported |
| 改名、修訂、Calendar／PIT 證據改變 | 重現 Key 改變；合法同 UUID 改名不改排序 |

另覆蓋重複日期、非法單位／數字、價格來源與還原基礎混合、未知發布時間、Future Revision、重疊身分、無成交證據、缺分母、跨年與不同 N。Facade 測試核對全數 24 個登錄項目、所有預設值、錯誤參數、版本、純 Domain 匯入及輸出金融字串。

執行方式：

```bash
# Repository 根目錄；不需資料庫、網路或 Migration
.tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_broker_flow_analytics_contracts.py \
  apps/api/tests/test_broker_flow_flow_calculations.py \
  apps/api/tests/test_broker_flow_proxy_calculations.py \
  apps/api/tests/test_broker_flow_engine.py -q

.tools/api-venv/bin/python -m pytest apps/api/tests \
  -m 'not integration and not network' -q

cd apps/web
npm test
npm run typecheck
npm run build
```

## 6. 本次實測結果

以下為本次實際執行結果：

| 檢查 | 結果 |
| --- | --- |
| H4 四個純 Domain 測試檔 | 223 passed |
| Backend 非整合／非網路回歸 | 3,259 passed；573 integration／network deselected |
| Vitest | 20 files、352 tests passed |
| TypeScript Typecheck | 通過 |
| Next.js Production Build | 通過，既有首頁、Proxy 與個股路由正常產出 |
| OpenAPI Snapshot | 包含於 Backend 回歸；沒有新增或修改 HTTP Contract |
| PostgreSQL／Migration／Docker／Playwright | 本次未執行；沒有修改 Persistence、Runtime、API 或畫面 |

可用的原始執行 Log 保存在本機忽略目錄 `.tools/validation/phase-2h4/`；不將依賴告警當作測試失敗。Backend 的 Starlette/httpx 棄用提示屬既有依賴，未進行無關套件升級。

## 7. 真實資料、Blocker 與下一階段前置

本階段沒有下載、取得或匯入任何正式分點資料，沒有寫入正式資料庫，也未重啟回補 Worker。Synthetic 測試筆數不能當真實資料覆蓋率。既有行情及籌碼資料未由本次程式修改；未重新執行正式資料庫覆蓋盤點。

H1 [來源查證](phase-2h-data-sources.md)中的自動下載／公開展示權限制保持 **Blocked**，沒有因為完成算法、持有離線檔案或可執行測試而解除。沒有新增可識別個人／中實戶／大戶的官方來源。分點交易是經由該分點承接的流量，不能解讀為特定投資人或實際庫存；Proxy 名稱必須保留方法與參數。

後續 H5 需另行授權才開始：在同一 PostgreSQL 唯讀 Snapshot 組裝公開 Reader、確認 Source／Scope 選擇與合法完整母體、載入足夠的核實日曆與 Streak 歷史，再建立 Query Application、HTTP Limits、Pydantic／OpenAPI、隔離 PostgreSQL HTTP Tests。正式資料驗收仍需適當授權、已核實真實報表／身分對照及來源修訂證據。本次不提前實作 H5／H6。
