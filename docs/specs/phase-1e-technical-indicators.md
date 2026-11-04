# Phase 1E — Technical Indicator Engine

## 邊界與共用介面

新增 `modules/technical`，沒有新增 DB table／Migration、依賴套件、行情下載、排程、選股器、回測、AI 分析或下單。既有 canonical／revision／ingestion 模型不變。

| 檔案 | 責任 |
| --- | --- |
| `technical/domain/models.py` | IndicatorEngine Protocol、輸入／輸出 frozen dataclasses、參數規則、算法版本及精度常數 |
| `technical/domain/engine.py` | 五種指標的唯一公式實作；純 Python，不 import FastAPI／SQLAlchemy／供應商 |
| `technical/domain/history.py` | 歷史輸入 snapshot／Source Port |
| `technical/application/service.py` | 驗證價格基準、讀取歷史、呼叫 engine、輸出篩選與 metadata |
| `technical/infrastructure/history.py` | 透過 market_data 公開 Application facade 實作歷史 Port |
| `market_data/application/indicator_history.py` | 在 market_data 邊界內取得 canonical／calendar 並按市場本地日期聚合 |
| `technical/api/` | FastAPI GET route、Pydantic schemas、dependency composition |
| `web/src/lib/technical-api.ts`／`technical-types.ts` | 同源 client、TypeScript contract／runtime guards |
| `web/src/components/indicator-workspace*` | 指標開關、參數、載入／錯誤／暖機、取消過期回應 |
| `web/src/lib/indicator-chart.ts`／`stock-chart*` | 只轉換後端結果、SMA／EMA／VMA 疊線、RSI／MACD pane |

API → Application → Domain；Infrastructure 實作 Domain Port。Technical 不讀其他模組的私有 Infrastructure／資料表。API 使用既有 MarketQueryService 的唯讀 REPEATABLE READ transaction、SQL／lock timeout；不在 request 抓第三方行情。

其他未來功能必須呼叫相同 `IndicatorEngine.compute()`，不能複製公式：

```python
from datetime import date
from decimal import Decimal
from stockapp.modules.technical.domain import DecimalIndicatorEngine, IndicatorSample

# 明確標記為人工計算示例，非真實行情。
samples = [IndicatorSample(date(2026, 1, day), Decimal(value), 100)
           for day, value in [(1, "1"), (2, "2"), (3, "3")]]
result = DecimalIndicatorEngine().compute(samples, "ema", {"period": 3}, price_basis="raw")
assert result.points[-1].values["ema"] == Decimal("2.000000000000")
```

## 版本、參數與數值策略

算法版本 **1.0.0**；參數預設值展開後完整回傳。公式、seed、缺值、平盤 RSI、精度策略改變時需升版並更新固定數值測試，不能以同版本悄悄改變數值。

| indicator | parameters／預設值 | 輸入欄位 | 第一個完整結果需要的連續樣本 |
| --- | --- | --- | --- |
| sma | period=20 | close | N |
| ema | period=20 | close | N |
| rsi | period=14 | close | N+1 |
| macd | fast_period=12, slow_period=26, signal_period=9 | close | slow+signal−1；MACD line 本身在 slow 根後可用 |
| volume_sma | period=20 | volume_shares | N |

期數僅接受1–250的 Python／JSON整數；拒絕 bool、float、小數、字串及未知參數。MACD fast<slow。JSON query 最多512字元，拒絕重複 key、非object、NaN／Infinity。Period1 支援直接觀察值；RSI1仍須一筆價格變化。

價格輸入必須為正且有限 Decimal 或明確 null；成交量為非負整數股或 null。日期為嚴格遞增且唯一的市場本地 `date`，拒絕 datetime／亂序／重複。不把 float 引入金融計算。

私有 `Decimal Context(prec=50, rounding=ROUND_HALF_EVEN)` 隔離呼叫者的 precision、traps 與 exponent 設定。內部 recurrence 不 quantize；**只在輸出**量化至小數12位，JSON Decimal 仍為字串（可能包含 `0E-12`），不將捨入輸出帶回下一期。無法表示的輸入回明確驗證錯誤，不默默使用 Infinity。負零正規化成零。

成交量均線可能有小數股，是每期平均，不截成整數，也不換算張數。前端只有圖表像素定位轉 number，數值標籤與 tooltip 保留原始 Decimal 字串；拒絕不安全或會碰撞的繪圖轉換。

## 公式與初始化

以下 x_t 是目前 timeframe 的 close；volume_sma 則將 x_t 換成該根聚合 `volume_shares`。N 是 K 棒數，不是日曆日數。

- **SMA**：`SMA_t = Σ(x_{t−N+1}…x_t) / N`。前 N−1 點 null。
- **EMA**：α=`2/(N+1)`；第 N 點以首 N 筆 SMA 初始化，其後 `EMA_t = EMA_{t−1} + α(x_t−EMA_{t−1})`。不以第一個 close 作種子。
- **RSI（Wilder）**：Δ=`x_t−x_{t−1}`，gain=max(Δ,0)，loss=max(−Δ,0)。首 N 個變化的算術平均作 avg_gain／avg_loss；其後各自 `(previous_average*(N−1)+current)/N`。RSI=`100−100/(1+avg_gain/avg_loss)`。只有 gain 時100、只有 loss 時0；gain與loss皆0時，本版本明確定義為**50**。這是專案的平盤約定，不宣稱與所有 library 相同。
- **MACD**：fast、slow EMA 都按上述方式，從同一序列的首筆開始各自 SMA seed。line=`EMA_fast−EMA_slow`；signal 為第一批可用 line 值的 SMA-seeded EMA；histogram=`line−signal`，**不乘2**。line 可在 signal 暖機時先回傳，signal／histogram仍null。
- **Volume SMA**：與 SMA 共用 `_MeanWindow`，僅輸入欄位不同；0股是有效觀察，null不是0。

公式參考 [TradingView RSI 說明](https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/) 與 [TA-Lib EMA](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_EMA.c)／[MACD](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_MACD.c)。本專案明定自己的 seed／flat／缺值與精度，不宣稱 TA-Lib 或 TradingView 的逐位相容性；未複製外部原始碼。

固定數值測試不是只檢查程式能跑。例如 EMA3 輸入 `[1,2,3,8,5,10]` 回 `[null,null,2,5,5,7.5]`；RSI3 對 `[10,11,10,12,11,14,13]` 回 `[null,null,null,75,54.545454545455,79.591836734694,62.4]`。MACD2/3/2 使用獨立分數驗證 signal種子2/3、histogram1/6，以及後續負柱；另驗證重複小數不得先捨入再遞迴。

## 缺值、交易日與週期

- 純 engine 不猜日期缺口是不是交易日；只計算呼叫者提供的觀察／明確缺值，週末假日不灌入0或前收。
- Source 對 canonical 及同市場 calendar 做有界讀取。已知 OPEN／HALF_DAY 且沒有 bar 時插入 **null輸入槽**，不是虛構行情；CLOSED 日不增加觀察。未知 calendar 不猜星期幾，不宣稱 coverage 完整。
- 選定欄位 null 時，該指標所有種子／遞迴狀態重置。該點 `missing_input`，後續重新暖機；不把兩段行情偷偷接起來。
- 週／月先使用 Phase 1C 的市場本地日期聚合：first open/max high/min low/last close/sum volume，再計算指標。週一至週日、日曆月分組，不拿日 SMA 再平均冒充週 SMA。
- 一個週／月若有已知缺失 constituent session，整個該期輸入設null；整段已知缺失期間也有null槽。未來 OPEN session（>to）不造成目前部分週／月被誤判缺資料。
- 尚未結束或被 to 截在期間中的週／月可計算觀察值，但 `is_provisional=true`。日曆不完整與暫定期間是不同事實，不能因能算出數值就聲稱歷史完整。
- 指標線在 null 處切成不同 LineSeries，避免 library 跨 whitespace 連成假線。缺日不延伸價格時間軸、不畫0柱。

## 初始化歷史與查詢限制

先讀從最早**已保存** canonical 日期到 `to` 的全部前綴，聚合、計算，再按返回 anchor `trade_date` 的 inclusive from/to 篩選；不以 from 重新 seed。相同 to／資料版本下，縮小 from 不改變重疊點的 EMA／RSI／MACD。週／月 anchor 是最後觀察日；整段缺失時則是最後已知開市日的 null 標記，不能當作行情。

只讀 `trade_date<=to` 的價格。這是計算上的日期截界，不是 point-in-time vintage 回測保證：目前讀的是**當前 canonical revisions**，較後匯入的歷史修訂仍可能改變過去指標，且 corporate actions／歷史完整度未驗證。

輸出範圍最多3660個日曆日、最多1000點；計算前綴最多20,000個日行情／含gap聚合輸入、36,525日曆日。超過時回422，**不截斷歷史後暗自重新 seed**。空資料回200、points=[]。來源讀取在同一唯讀 snapshot 中，不受查詢中途寫入影響。

metadata 的 full_available_history 是完整**已保存前綴**，不代表官方完整上市歷史。`history.completeness` 與 `calculation.history_completeness` 均保留 not_verified。沒有 Calendar 的缺日無法自動識別，故指標僅代表觀察序列。

## API 與前端

`GET /api/v1/stocks/{instrument UUID}/indicators`

Query：timeframe=`1d|1w|1M`、indicator、parameters(JSON)、from/to（預設市場本地 today 與往前365天）、price_basis=`raw|adjusted`。回應 Pydantic `IndicatorResponse`，已更新 `apps/api/openapi.json`。

回應包含 instrument/timeframe、indicator/algorithm_version/parameters、price_basis/field、required_samples、points/count/from/to、precision、calculation、freshness/history、price_adjustment=unadjusted、data_kind=daily_snapshot。每點含 Decimal字串/null 的 values、ready/warmup/missing_input、is_provisional。

目前 API 僅有 raw canonical history；adjusted 明確422 `adjusted_price_unavailable`，不能將未還原價格更名成已還原。純 engine 可接受 caller 提供且可追溯的 adjusted 序列並標記，但本階段沒有產生 adjusted 資料或 API。

404未知／非股票、422參數／範圍／計算上限／adjusted、503DB或儲存資料異常；使用既有安全錯誤外觀，不轉送SQL、DSN或原始上游文字。

前端沿用 `/api/market/*` 同源 GET proxy，增加 indicators allowlist／safe codes。開關預設關閉；選取才查詢，draft參數輸入不觸發計算，按套用才生效。週／月圖的查詢起日對齊週一／月初，與完整起始 bucket 的指標一致。切換 timeframe 保留開關／參數，取消舊回應；行情重新載入時重新計算指標。

SMA／EMA 在價格 pane、volume_sma 在 volume pane；RSI 含30／70參考線，MACD含line／signal／正負 histogram。暖機不偽裝為0，部分 MACD line 可先顯示；指標錯誤仍保留價格圖並可獨立重試。每次指標資料改變會重建圖表，可能重設縮放視窗，尚未加入 viewport 保存。

## 驗證命令與結果

```bash
npm test
npm run typecheck
NEXT_TELEMETRY_DISABLED=1 npm run build
docker compose config --quiet
docker compose up --build --wait --wait-timeout 300
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -m 'not network' -q -p no:cacheprovider
docker compose run --rm backend python scripts/export_openapi.py --check
npm run test:browser --workspace @stockapp/web
```

Browser 需要已啟動前後端／已保存真實2330；一般環境先 `npm exec --workspace @stockapp/web -- playwright install chromium`（Linux缺libs可用 `--with-deps`）。此 WSL沿用Phase1D工作區隔離 browser/libs/fonts，實際指令：

```bash
PATH="$PWD/.tools/node/bin:$PATH" \
LD_LIBRARY_PATH="$PWD/.tools/browser-runtime/usr/lib/x86_64-linux-gnu" \
FONTCONFIG_FILE="$PWD/.tools/browser-runtime/fonts.conf" \
PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers" \
npm run test:browser --workspace @stockapp/web
```

2026-11-04（Asia/Taipei）實際結果：

| 驗證 | 結果 |
| --- | --- |
| Domain固定數值／邊界 | **89 passed**，包含分數預期值、seed、warmup、reset、Decimal context、平盤RSI、部分MACD |
| API／application | **41 passed**，含五種數值、日週月、from不重seed、to截界、缺值及查詢上限 |
| 新PostgreSQL HTTP整合 | **6 passed**，私有隨機schema，數值／週期／重複資料／gap與唯讀驗證 |
| 全部後端回歸 | **640 passed，2 deselected**（官方network tests未執行）；既有1項Starlette/httpx deprecation warning |
| Vitest | **177 passed**，13個test files，包含既有Phase0–1D測試 |
| TypeScript／Production Build | 通過；本機 `NEXT_TELEMETRY_DISABLED=1 npm run build` 及Docker build均成功 |
| Chromium | **6 passed（9.2秒）**：原3項、真實行情設定／切換／手機、指標錯誤重試、固定合成資料完整曲線 |
| OpenAPI／Schema | export `--check` 通過；alembic check無差異，沒有新migration |
| Compose／資料 | 四服務healthy；透過同源proxy實測全部五種指標，canonical行情仍**1973**筆 |

離線 fixtures 全部明確標記synthetic，PostgreSQL整合只用隨機schema，不污染public資料。Browser的真實資料測試只讀先前保存的2330；合成資料測試僅用Playwright route攔截，股票名稱顯示「合成測試股票（非真實行情）」，驗證SMA／EMA／RSI／MACD及成交量均線的數值與真實canvas繪圖，包括負histogram；沒有DB寫入或交易所請求。桌機／手機及合成曲線截圖保存在gitignored `apps/web/test-results/`，已檢視。

瀏覽器錯誤測試首次因Next.js route-announcer同樣使用alert角色而造成locator ambiguity，限定應用程式區塊後全部通過。沒有執行遠端GitHub Actions，也沒有執行外部官方網路測試。

## 尚存限制

- 本機目前每檔僅少量真實歷史資料，預設指標多為warmup；未向正式資料庫補入假值。SMA／EMA／volume_sma設period1可呈現當筆資料，RSI／MACD仍需要較長歷史。
- 尚無adjusted history／公司行動因子、完整官方交易日曆／上市歷史，因此不宣稱全部歷史完整或可作vintage回測。
- 指標數據更新會重建圖表，可能重設縮放／平移視窗；數值與設定保留，viewport保存未納入本Phase。
- 沒有commit、push、部署、進入下一Phase，沒有開發策略／選股／AI／下單。
