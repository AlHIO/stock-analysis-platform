# 最近十年台灣日行情回補

使用者確認範圍：2016-11-05 至 2026-11-05（台北日期、含起訖），上市／上櫃及 ETF、權證等證券的每日 OHLCV 與基本資料。不新增法人、財報、持股、即時交易、回測或調整股價。

本次建立可執行的官方歷史資料管線與持久化工作，不把「工作啟動」等同十年資料已全數入庫。已到期／下市證券的歷史身分仍有未確認的來源，完整 raw 留存與已核實 canonical 分開記錄。

## 官方歷史來源查證

查證日期為 2026-11-05。少量實測以日期、HTTP、實際 schema 與來源說明共同驗證，未使用非官方資料或測試 fixture 代替正式行情。

| 市場 | 已確認 GET endpoint | 日期與範圍 |
| --- | --- | --- |
| TWSE | `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALL&response=json` | 官方頁說明起自 2004-02-11；本次實測 2004-02-11、2016-11-07、2020-01-02、2026-11-04 均回指定日期 |
| TPEx | `https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date=YYYY%2FMM%2FDD&response=json` | 現行頁的舊格式分界另有入口，本 Adapter 保守限制 2007-04-23 以後，涵蓋本次十年；已實測 2016-11-07、2020-01-02、2026-11-04 |

原始頁與資料界線：[TWSE 每日收盤行情](https://www.twse.com.tw/zh/trading/historical/mi-index.html)、[TPEx 上櫃股票行情](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pricing.html)。TPEx 2007-01-02 在現行 endpoint 雖回 HTTP 200/stat=ok，但沒有行情，官方頁提供獨立舊格式入口，因此不能宣稱此 endpoint 涵蓋所有早期日期。

實測 2016-11-07 的 TWSE 有 10,639 行、TPEx 有 4,115 行；2026-11-04 分別有 40,871／13,626 行。這些是特定回應的觀察，不是未來每日筆數保證。TWSE 2020-01-04 回已知無資料訊息；TPEx 2016-11-05 回指定日期但兩張行情表均空。`no_data` 僅指該次來源沒有回傳資料，不能自動寫成 `TradingCalendar.CLOSED`。

TWSE 依 OHLCV 欄位集合選表，不固定索引。TPEx 主行情與管理股票表都保留，映射 `代號/名稱/開盤/最高/最低/收盤/成交股數/成交金額(元)/成交筆數`，並保留原中文欄位與表格名稱。成交股數已是基礎數量，不能因 envelope 的 `flagField` 或買賣量以張揭示而再乘 1,000。兩市場皆嚴格核對回應日期與請求日期，錯日、未知狀態、破損 envelope 不會冒充空資料或補造日期。

## 歷史身分與完整性

官方[證券編碼原則](https://twse-regulation.twse.com.tw/TW/law/DOC01.aspx?FLCODE=FL033103&FLNO=1)允許認購售權證代碼重複使用。行情報表沒有 ISIN、CFI、發行／到期日，不能只由代碼或名稱把不同發行期合併到 `(market_id, symbol)`。目前 ISIN 目錄又不涵蓋十年內所有已到期／下市證券。

- 目前名錄內的證券，必須有已確認掛牌日，且歷史交易日不早於該日，才可使用該 Instrument。
- 目前名錄以外的代碼、掛牌日無法確認、或交易日早於目前發行期的同代碼：`out_of_scope` + `historical_identity_unresolved`，計入 `unresolved`；保存完整 raw／逐列紀錄，不新增猜測的 Instrument 或 bar。
- 今日名錄不是歷史預期 universe，因此不以今日全部成員生成歷史 `missing` 列。
- 較舊 metadata 不回寫較新分類／幣別／單位；共用名錄使用其原取得時間，不以每天的新行情下載時間把舊名錄當成新資料。
- OHLCV 與 turnover 使用既有 Decimal／NUMERIC、基礎股數或單位、canonical PK 與 revision fingerprint，不補零、均價、前收價或模擬行情。
- 基本資料是工作開始時取得的官方最新名錄；**尚未實作十年逐日生效的歷史基本資料**。MOPS「含下市權證」入口存在，但其資料契約、ISIN 與完整歷史覆蓋未核實，不能宣稱已完成。

所有 job 回報 `canonical_coverage=current_registry_lifetimes_only`，每日 stats 有 `historical_scope_limited=1`。即使所有日期已嘗試處理，結果仍保守回報 `completed_with_gaps`，不以 survivorship subset 宣稱全市場歷史完整。部分日成功只表示該次回應中可處理的列已驗證，不證明歷史 universe 完整。

## 分層與儲存

| 檔案／表 | 作用 |
| --- | --- |
| `domain/history.py` | 純 Python job/day 模型與 Store Port |
| `application/history.py` | 最近日期優先、逐日執行、續跑、有限失敗與儲存增量防護 |
| `application/history_normalization.py` | 純歷史 envelope／日期／欄位解析 |
| `infrastructure/history_providers.py` | 帶明確日期的官方 Adapter，沿用 timeout、有限 retry、host limiter |
| `history_jobs` | 日期範圍、市場、狀態、建立／更新時間、DB 基準大小及增量預算 |
| `history_days` | `(job_id, market, trade_date)` PK、狀態、嘗試次數、run_id、錯誤與處理時間 |
| `ingestion_requests.catalog_run_id` | 同市場／同來源歷史 run 的共用名錄來源，FK RESTRICT；避免逐日重存名錄 bytes |

Migration `0004_history_backfill` 新增兩張表、history mode 與 catalog reference。來源獨立為 `TWSE_HISTORY`／`TPEX_HISTORY`，市場與原 Instrument UUID 延用；舊版 sync/replay 仍可運作。新表有日期、狀態、生命週期、計數、FK／Index／CHECK。空表可 downgrade；有 job、history run 或 catalog reference 時拒絕有資料損失的降版。

每個有資料日期先保存 quote raw，再以既有逐列 savepoint 匯入 canonical／revision。每個市場只保存一次完整已解析名錄與額外幣別 metadata，後續歷史 request 引用原 run；不複製每天一份大名錄，也不在共用原始回應中改寫日期。回放能解析 reference、保留原取得時間，且不連網。

## 工作語意與執行

完整指令見 README 的「最近十年歷史回補」。`history plan` 只建立工作，不發出外部請求；`history run --job UUID` 從最近日期往前處理；`history status --job UUID` 查詢計數、累積 stats、最新工作及剩餘日期；`history replay` 離線重播已有 archive。

預設最多 3,660 個日曆日，涵蓋十年；兩市場本次為 7,306 個市場／日期工作。所有日曆日都會查詢，不以星期幾直接假定休市。當日尚未發布的空回應會留為 no_data；之後發布的新資料可用 `sync latest` 或建立指定單日 history 工作取得，不能把先前 no_data 當成永遠沒有交易。

日期狀態：pending、running、succeeded、partial、no_data、failed。Partial 保留有效列，預設視為該次處理的終態，避免無限下載無法確認的歷史證券；failed 日期只有明確 `--retry-failed` 才重試一次。Job 執行全程持有 schema／job 隔離的 PostgreSQL advisory lock，來源每日操作仍持市場鎖，避免同一工作或市場重疊寫入。重新啟動取得鎖後，將上次未完成 running 日期恢復 pending；原始回應與 canonical PK 保護中斷後重送。

每 host 預設至少 5 秒間距、最多 3 次有限重試與既有 timeout／response-size 上限。400／401／403／404／302 等永久錯誤不跨日期反覆嘗試；連續三個日期工作失敗時停止本次 runner。沒有繞過封鎖、登入、驗證碼或使用非官方替代資料。

`--max-days`／`--max-seconds` 都在日期交易之間檢查；完成的日保留，未完成日等待下次續跑。以建立工作時 `pg_database_size` 為基準，預設資料庫增量達 100 GiB 就停止，不刪資料。status 同時提供 current_database_bytes／database_growth_bytes。預算可在 plan 以 `--max-growth-gb` 設定 1–1000 GiB；它涵蓋共享 DB 的所有寫入，不是精準單一 job 配額，也不能保證 Windows 實體磁碟空間。執行時間與磁碟使用以實際進度衡量。

Compose `history-worker` 僅在 `history` Profile 明確啟動；不公開 HTTP port，不執行健康 API，無自動排程，也不自動重啟部分失敗的工作。逐日 JSON progress 可由 `docker compose --profile history logs --tail=20 history-worker` 讀取。Docker Desktop 或主機停止後，需以原 job UUID 重新啟動；checkpoint 仍保留於 PostgreSQL。

## 授權與驗證

使用官方日期查詢／匯出來源，未確認完整歷史組合的 OGDL 授權，CLI 不宣稱 blanket open-data license。下載與再利用依 [TWSE 使用條款](https://www.twse.com.tw/zh/terms/use.html)、[TPEx 使用條款](https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html)。需要完整歷史證券身分或授權交付時，應另核實官方歷史名錄／資訊商店契約，不由目前可連線就宣稱任意資料散布權。

測試 fixture 全為合成資料，只進入測試的隨機 PostgreSQL schema。測試涵蓋兩市場欄位、日期、單位、no_data、timeout/retry、原始 bytes、真實 PG Migration／安全降版、歷史權證代碼重用防護、共用名錄、離線 replay、重複寫入、續跑與儲存／時間限制，以及既有 Phase 0–1E 回歸。實際匯入另記 job/run，不以測試結果當作十年已完成的證明。

## 本次實際匯入與工作

首先以使用者範圍起點的 2016-11-07 進行兩市場真實驗證，job `a8d53a06-f3f9-45b9-8a41-32178e6999b1`：

| 市場 | 原始行情列 | 有效 canonical 新增 | 無成交 skipped | 缺價 rejected | 歷史身分 unresolved |
| --- | ---: | ---: | ---: | ---: | ---: |
| TWSE | 10,639 | 898 | 19 | 4 | 9,718 |
| TPEx | 4,115 | 612 | 10 | 9 | 3,484 |

合計新增 1,510 筆，未確認的 13,202 列完整保留原始回應與逐列理由。兩個日期工作皆 partial，job 為 completed_with_gaps，沒有宣稱全市場當日完整。

- TWSE run：`494c24d0-d0a2-40d1-8b17-3caf5be771d5`
- TPEx run：`dc7adeea-b1a5-4430-9a9a-bed50f56f840`

另已建立完整十年工作 `efa06b94-86ec-41cf-a81a-cbf746b3ab35`，日期為 2016-11-05 至 2026-11-05、兩市場共 7,306 個 checkpoint，預算為 DB 增量 100 GiB。這個工作和單日驗證工作分開記錄，可用下列指令查詢與啟動／續跑：

```bash
docker compose run --rm backend stockapp history status --job efa06b94-86ec-41cf-a81a-cbf746b3ab35
HISTORY_JOB_ID=efa06b94-86ec-41cf-a81a-cbf746b3ab35 docker compose --profile history up -d history-worker
docker compose --profile history logs --tail=20 history-worker
```

狀態與累積筆數隨背景工作變動，以 status 輸出及 PostgreSQL checkpoint 為準；不要將本文件建立時的數字當成全十年已下載完成。

本機已啟動 `stockapp-history-worker-1`，首個完成的 checkpoint 為 TPEx 2026-11-05：來源 13,679 列、有效新增 5,664、無成交 8,009、缺價拒絕 6、歷史身分 unresolved 0，run `b49c10ee-2a53-4982-b423-317214b08caa`。該日為 partial 並繼續下一日，非整批失敗或十年已完成。首次狀態快照為 processed 1／running 1／pending 7,304，之後會持續變動。

後續已確認 2026-11-05 兩市場共新增 23,760 筆有效日行情，worker 已往 2026-11-04 繼續處理。另以資料庫唯讀核對 2330 的 2016-11-07 歷史收盤為官方原始回應的 Decimal 189.00000000，來源為 TWSE_HISTORY；全庫重複 canonical key 為 0。這些均為當次核對結果，不代表尚未處理的十年日期已完整。

## 本次驗證指令與結果

```bash
cd apps/api
RUN_INTEGRATION=1 ../../.tools/api-venv/bin/python -m pytest -m 'not network' -q
# 889 passed，2 個獨立 network tests 未執行
cd ../..
PATH="$PWD/.tools/node/bin:$PATH" npm test
# 190 passed
PATH="$PWD/.tools/node/bin:$PATH" npm run typecheck
PATH="$PWD/.tools/node/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 npm run build
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
docker compose config --quiet
docker compose --profile history config --quiet
docker compose run --rm backend alembic upgrade head
docker compose up -d --wait
```

Typecheck、Production Build、OpenAPI snapshot、Compose 兩種 Profile 設定檢查皆通過。Migration 已在本機套用至 `0004_history_backfill`，原有四服務 healthy；獨立 worker 沒有 HTTP healthcheck，依 job checkpoint 查詢執行狀態。Chromium／Playwright 在真實歷史資料入庫與背景工作啟動後 **6 passed**。網路測試與離線測試分開；本次另以官方來源完成前述真實指定日期下載／寫入，沒有使用 fixture 作正式行情。

最後新增的快取 metadata 回歸測試曾因測試存取已關閉 Session 的 ORM 物件而失敗；已改用 Domain UUID，完整重跑結果為上述 889 passed。既有 Starlette/httpx deprecation warning 保留，未進行無關依賴升級。未 commit、push 或進入其他資料功能階段。
