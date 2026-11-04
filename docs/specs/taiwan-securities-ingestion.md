# 台灣上市／上櫃全證券最新快照

本次擴充既有 Phase 1B 管線，新增明確的 `securities` 範圍，不建立排程、不新增歷史下載能力。查證日期為 2026-11-05（Asia/Taipei）；研究快照的實際交易日為 2026-11-04，不代表每天筆數、發布時間或即時性保證。

## 操作

```bash
docker compose build backend frontend
docker compose run --rm backend alembic upgrade head
docker compose up -d
docker compose run --rm backend stockapp sync latest --market all --scope securities
docker compose run --rm backend stockapp runs --limit 5
docker compose run --rm backend stockapp runs --id <RUN_UUID>
docker compose run --rm backend stockapp backfill --market twse --scope securities --date 2026-11-04
```

`--market` 可選 `twse`、`tpex`、`all`；backfill 必須指定單一市場。省略 scope 保留原有 `stocks` 行為。退出碼 2 是 partial／失敗，已通過驗證的列仍保留，必須讀取 report，不能把退出碼 2 解讀為整批沒有寫入。沒有相符 archive 時明確失敗，backfill 不連網。

## 官方來源

| 市場／資源 | GET URL | 格式與用途 |
| --- | --- | --- |
| TWSE quotes | `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=ALL&response=json` | 官方每日收盤行情頁的全部類別匯出；多表 JSON，從欄位辨識 OHLCV 表，不依賴固定表格索引 |
| TWSE catalog | `https://isin.twse.com.tw/isin/C_public.jsp?strMode=2` | CP950 HTML，上市證券的 ISIN、CFI、分類及掛牌日 |
| TWSE fund_catalog | `https://openapi.twse.com.tw/v1/opendata/t187ap47_L` | 官方 ETF metadata；以完整名稱中的「加掛美元／人民幣」確認外幣報價類別 |
| TPEx quotes | `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` | 既有 OpenAPI 已包含股票、ETF、ETN、權證等，數量為基礎股數／單位 |
| TPEx catalog | `https://isin.twse.com.tw/isin/C_public.jsp?strMode=4` | CP950 HTML，上櫃證券分類 |
| TPEx currency_catalog | `https://www.tpex.org.tw/storage/eb_data/11401/11400000051.html` | 官方掛牌公告，明載 00687C 交易幣別為美元 |

TWSE [每日收盤行情頁](https://www.twse.com.tw/zh/trading/historical/mi-index.html)提供全部類別與匯出介面；[證券編碼入口](https://www.twse.com.tw/zh/products/code/announcement.html)連結官方 ISIN 目錄。這不是 OpenAPI 的同名 MI_INDEX 指數統計資源。所有實際請求只取最新快照，不傳歷史日期參數。

研究原始快照中，TWSE 名錄 42,032 筆／行情 40,871 列，TPEx 名錄 13,911 筆／行情 13,626 列；大量權證沒有成交，因此「名錄數／行情列數」不等於「可建立 K 線數」。這些是特定快照觀察，不是執行成功門檻。

## 分類與單位

分類依 ISIN HTML 的官方區段，不能由代碼長度或名稱猜測：

| 官方分類 | instrument_type | quantity_unit |
| --- | --- | --- |
| 股票、創新板 | stock | shares |
| 特別股 | preferred_stock | shares |
| ETF | etf | units |
| ETN | etn | units |
| 上市／上櫃認購售權證 | warrant | units |
| 臺灣存託憑證 | dr | units |
| 不動產投資信託受益證券 | reit | units |
| 資產基礎受益證券 | abs | units |

`quote_currency` 是每檔證券的報價／成交金額幣別，市場的主要幣別仍為 TWD。股票與特別股以股為數量，ETF、權證等以各自基礎單位為數量；沿用既有 `daily_bars.volume_shares` 欄位名稱，讀取非股票時必須同時讀取 Instrument 的 `quantity_unit`。不乘以 1,000，也不依股票／ETF 每手交易單位換算。USD ETF 的 turnover 不可當 TWD 或自行匯兌。

外幣基金的代碼／名稱標記僅用來偵測需要佐證的情況，不直接決定幣別。TWSE 基金名稱中的「加掛美元／人民幣」與 TPEx 掛牌公告才是本次佐證。TPEx 若新增外幣 ETF，需新增官方 metadata 來源；未能確認時整份名錄驗證失敗，保存 raw 並保持既有 canonical data，不默認新臺幣。此限制避免投資標的是美債就被誤標為美元。

掛牌日晚於快照交易日的證券不建立該日行情，也不算缺列。未知分類、錯誤市場、重複代碼、缺少外幣佐證或破損名錄會明確失敗。ISIN／CFI 及原始分類保留在 catalog raw；目前不新增歷史名錄／代碼重用模型。

## Schema 與資料品質

Migration `0003_securities_metadata` 擴充 Instrument type CHECK，新增非空 `quote_currency`、`quantity_unit` 及 CHECK，並允許額外的 `fund_catalog`／`currency_catalog` raw 資源。舊列以市場幣別回填。Downgrade 僅允許不會丟失新種類、幣別、數量單位或額外 raw 的情況；否則明確拒絕，不刪除使用者資料。

`(market_id, symbol)` 維持唯一，修正分類仍保留 Instrument UUID、created_at 及既有 bars。舊公司目錄曾包含 TDR，全證券名錄會將其修正為 dr；後續股票範圍同步會將已確認的非股票記為 out_of_scope，不改回普通股。較舊 archive 不覆蓋較新的 metadata。

行情繼續使用 Decimal／NUMERIC、canonical PK `(instrument_id, trade_date)` 與既有 revision hash。每列 savepoint 保留有效列；重複來源代碼全部拒絕，不由列順序選價格。每次處理的原始 bytes、端點、SHA-256、HTTP 狀態與取得時間均先保存，解析只在記憶體加入 TWSE envelope 的交易日，不重寫 raw。

- 四價缺值且數量為零：skipped，保留 metadata，不建立零價 bar。
- 缺價但有成交量、部分 OHLC 缺值、零價、非法高低價：rejected，保留 raw 與原因。
- 名錄證券不在行情快照：missing，保留已掛牌證券 metadata，不補造價格或停牌狀態。
- 行情代碼不在名錄：out_of_scope 並計入 unmapped；run 回報 partial，保留其他有效列，不宣稱已完整分類。
- 債券殖利率、成交萬元、面額等非 OHLCV 資源：不轉成此 DailyBar 模型。

`TWSE_SECURITIES`／`TPEX_SECURITIES` 與舊版 `*_OPENAPI` 為獨立來源，市場識別仍相同。Backfill 同時按市場、來源及必要 extra resources 選取完整 archive，保留原取得時間，不以 replay 當作新鮮行情。Report 包含 received／accepted／skipped／rejected／out_of_scope／missing、canonical insert/update/unchanged、catalog_total／catalog_future 及各種類 accepted 計數。

若舊 archive 的分類、幣別或基礎數量單位與較新的 Instrument metadata 不符，該列拒絕寫入並保留原始回應，不能把舊價格默認解讀成新的幣別。真正的歷史幣別／單位變更仍需另行設計版本化模型，本次不宣稱完成。

## 授權、限流與能力界線

原始提供機關為 TWSE／TPEx，名錄由 TWSE ISIN 網站提供。本模式混合官方 OpenAPI、網站匯出及公告，**未確認完整組合的 OGDL 授權**；不將舊版股票 OpenData 授權延伸到全部網站內容。CLI 回報 `license: null` 與 [TWSE 使用條款](https://www.twse.com.tw/zh/terms/use.html)、[TPEx 使用條款](https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html?l=zh-tw)。下載方式及再利用依各來源條款；目前不建立大量歷史抓取、公開資料散布或商用授權保證。

公開規格未確認數字 quota 或 SLA。沿用每 host 預設 5 秒間距、30 秒 read timeout、最多 3 次有限重試、Retry-After 上限及 20 MB 回應上限；這是工程設定，不是交易所公布的限流額度。不繞過認證、驗證碼或封鎖；HTTP 403 等永久錯誤明確失敗。

本模式是上市／上櫃名錄及已確認來源的最新日行情；未涵蓋興櫃、未上市、所有債券／金融商品與完整歷史。官方名錄列出的 ABS 若無日 OHLCV，只有 metadata 與 missing 記錄。股票圖表與 technical API 維持 stock 範圍；非股票 metadata 可由 `/api/v1/instruments` 查詢。搜尋 API 支援 `instrument_type` 篩選並在分頁之前套用，股票介面不受大量權證擠掉搜尋結果。

## 驗證

固定 fixture 僅供測試，測試資料不得進入 public schema。離線來源解析／timeout／retry 測試與實際 PostgreSQL 的私有 schema 整合測試分開；另驗證 Migration、資料約束、分類修正、外幣／單位、無成交、冪等與隔離 scope 的 archive replay。實際匯入結果依當次 run report，不以 fixture 筆數宣稱真實資料已入庫。

### 2026-11-05 本機實際匯入

兩市場行情的原始交易日均為 **2026-11-04**。下載時的名錄已更新為 TWSE 41,883 列／TPEx 13,866 列，其中未到掛牌日的 799／176 列不納入該交易日。入庫證券 metadata 合計 54,774 檔；保留原有資料後 canonical 日行情合計 25,841 筆，重複 `(instrument_id, trade_date)` 為 0。

| 本次結果 | TWSE | TPEx |
| --- | ---: | ---: |
| 原始行情列 | 40,871 | 13,626 |
| 有效日行情 | 18,702 | 6,047 |
| 無成交／四價缺值，未建 bar | 22,169 | 7,571 |
| 缺價但有成交量，拒絕建 bar | 0 | 8 |
| 已掛牌名錄代碼未出現在快照 | 213 | 64 |
| 新增 canonical bar | 18,702 | 5,166 |
| 更新既有 canonical bar | 0 | 881 |

24,749 筆有效日行情包含股票 1,969、ETF 360、權證 22,364、特別股 26、ETN 14、TDR 10、REIT 6。ABS 4 檔只有 metadata，沒有可確認的日 OHLCV。修正了舊名錄的 10 檔 TDR 分類，保留原 UUID 及歷史 bars。USD ETF 00687C 的 30,000 基礎單位及美元報價／成交額已由實際入庫檢查確認，沒有套用每手 100 或 1,000 的倍率。

原始 sync run：

- TWSE：`6542ff6d-e389-4770-9f3e-743f78d1d8be`
- TPEx：`ba0a4408-fe53-4aa9-8055-f1e6eda8973f`

兩個 run 都因 missing／rejected 列回報 **failed + Partial coverage（退出碼 2）**，有效列已保留；不能宣稱完整行情或把缺失列當成停牌。各 run 有三份原始回應及逐列處理結果，可用前述 `stockapp runs --id` 查閱。當次 raw／canonical／metadata 已實際寫入本機 PostgreSQL，沒有使用測試 fixture 代替正式行情。

隨後使用最終版本 CLI 對兩市場各執行一次離線 backfill：TWSE replay `ce252e77-f01a-46a7-a68f-6401a0932701` 的 18,702 筆、TPEx replay `0bb1c2ec-0ed1-456a-bbe0-5520929efa05` 的 6,047 筆全部為 `canonical_unchanged`；兩次均新增／更新 canonical 0 筆、metadata 0 筆。保留原取得時間，沒有連網補抓，來源中的 missing／rejected 仍如實回報。重播後 canonical 總數維持 25,841，重複為 0。

最終程式驗證：

```bash
# repository 根目錄；等價 Docker 指令見 README
cd apps/api
RUN_INTEGRATION=1 ../../.tools/api-venv/bin/python -m pytest -m 'not network' -q
# 776 passed，2 個獨立 network tests 未執行
cd ../..
PATH="$PWD/.tools/node/bin:$PATH" npm test
PATH="$PWD/.tools/node/bin:$PATH" npm run typecheck
PATH="$PWD/.tools/node/bin:$PATH" npm run build
docker compose config --quiet
docker compose ps
```

Vitest 190 passed；Typecheck、Production Build、OpenAPI snapshot 檢查通過。獨立 network tests 未併入離線測試；本次另以真實官方資料完成上述下載及匯入。Compose PostgreSQL／Redis／backend／frontend 均 healthy，`/health/ready` 的 PostgreSQL／Redis 連線為 ok。Chromium／Playwright 6 passed，驗證入庫後的 2330 搜尋、日週月 K、指標、手機尺寸與失敗狀態。
