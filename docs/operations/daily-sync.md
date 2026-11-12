# 每日資料檢查與同步

依需求每日 **20:00（Asia/Taipei）** 執行。這是明確啟用的維運Worker，沿用現有Adapter、Normalizer、Raw／Audit、Revision及Canonical流程；沒有在GET API或網站開啟時抓資料，也不啟動歷史回補Worker。

## 啟用指令

在WSL的Repository根目錄執行。先保留現有`.env`，新增設定可參考`.env.example`，不要覆寫你的連線設定。

```bash
# 只建置新的Worker；不重建既有網站／歷史Worker
docker compose build daily-worker
# 查看版本，再套用新增的兩張排程紀錄表；不刪除行情／籌碼表
docker compose run --rm daily-worker alembic current
docker compose run --rm daily-worker alembic upgrade head
# 背景執行，每天台灣時間20:00檢查
docker compose --profile daily up -d daily-worker
# 查最近紀錄（不下載）及執行Log
docker compose run --rm daily-worker stockapp daily status --limit 5
docker compose --profile daily logs --tail=50 -f daily-worker
```

這次程式交付沒有替正式資料庫Migration，也沒有自動啟動排程；上列是實際啟用步驟。Profile預設關閉，不開HTTP Port，啟用後`restart: unless-stopped`。Docker／電腦／WSL停止或休眠時不能執行；恢復後補做最近一次到期檢查，不假稱在關機期間有同步。

如需現在檢查一次，可執行：

```bash
docker compose run --rm daily-worker stockapp daily run
```

這是獨立手動Cycle，`scheduled_for=null`；不會消耗今天20:00的排程。它會下載已核准來源，不是Dry Run。請勿與歷史Worker或其他手動同Host匯入同時執行；每日Worker之間有PG全域鎖，但舊CLI／History的Host限流尚未跨產品共用。

## 哪些資料會更新

| 工作 | 來源／模式 | 限制 |
| --- | --- | --- |
| `market.twse` | TWSE股票OpenAPI最新日行情與主檔 | 使用來源交易日，不用今天補日期 |
| `market.tpex` | TPEx股票OpenAPI最新日行情與主檔 | 同上；已保存有效列仍依既有策略冪等寫入 |
| `institutional.tpex` | 已核准TPEx法人最新快照 | 缺dealer明細／交易口徑，仍可能只有Raw／candidate，不能冒充完整Canonical |
| `ownership.tdcc` | TDCC官方最新CSV週報 | 週資料保持週統計日；缺dated identity時Raw／Audit保存、正式報表unresolved |
| `margin.twse` | 已核准TWSE Margin OpenAPI | 原來源無交易日，需同Payload可信日期與單位Evidence，否則不晉升Canonical |
| `margin.tpex` | 已核准TPEx Margin最新快照 | 張股轉換須有該證券／日期的Evidence，不能一律乘1000 |
| `lending.tpex` | 已核准M6借券賣出最新快照 | 不替代借券未還；Channel／Scope未核實的分析結果保持受限 |

**ETF／權證等全證券網站匯出與ISIN組合來源、TWSE T86、借券未還、未核准歷史端點沒有被自動啟用。** `--market-scope securities`會將行情工作記為blocked，不會繞過限制。預設`stocks`使用已核准股票OpenAPI；這不表示所有台股商品皆自動覆蓋。

已核准下載不等於已具備整批Canonical條件。尤其未提供有效Evidence時，籌碼可取得最新Raw與官方統計日，但網站仍可能顯示較早Canonical／Unavailable；**不能把下載成功當成網站已完整更新**。

## 如何判斷最新

每天都取得來源的latest快照，再交由原管線驗證及冪等Upsert；不只比較日期就跳過，因為同一天也可能修訂。週末／休市可能回上一個交易日或同一週，保留官方日期及unchanged，不補假日資料或零。

每個工作紀錄包含：

- `before`／`after`：透過模組公開Reader讀取來源層的Canonical日期／筆數、已知官方Raw日期與Run。Source限定的日期不等於每檔股票或全市場皆完整。
- `reports[].official_date`／`run_id`／`stats`：這次既有匯入流程回傳的真正統計日期、Run與處理數；可取得時保留選中Raw的SHA-256。
- `canonical_date`：該來源已晉升Canonical的最新日期；不是下載日期。
- `freshness=matches_received_snapshot`：只表示成功匯入的Canonical符合這次收到的官方快照；**不保證官方發布SLA、完整交易日曆、全市場或十年歷史**。
- `not_verified`／`partial`／`blocked`／`failed`：官方日期未知、原始資料受限、Evidence不足、來源失敗等情況明確保留，不改成成功。

股票Reader只查該來源，避免每個工作呼叫昂貴的全市場data/status。籌碼公開Status提供的`official_raw_date`是「目前已知的最大已解析統計日」，不一定是最後一次HTTP Body日期；`official_date_semantics`會說明此差別，實際新快照以本次Run報告為準。未知Hash為null，不杜撰Hash或發布時間。

## 排程、重試與中斷

- 用台灣當地日期計算每日20:00，不依賴容器系統時區或UTC日期。
- 啟動時只補「最近一個已到期場次」；上午啟動可能立即檢查昨日場次，當天20:00仍會再檢查。離線多天不逐日冒充歷史回補，latest-only來源也不能補出那些缺日。
- 同一排程日期唯一Cycle；完成的來源不重抓，重啟後從尚未完成／可重試的工作續跑。已完成Cycle不因查Status或重啟而改寫完成時間。
- 對可確認的Timeout／429／5xx／連線等暫時錯誤，每個工作每日最多3個持久化嘗試，間隔預設15分鐘；每個嘗試中的原HTTP Adapter另有最多3次HTTP重試。Schema／權限／Evidence／Partial不無限重試。
- 全部工作依序執行，工作間至少5秒，原Adapter保留Timeout／Retry-After／大小上限。這是工程節流設定，不是官方公告配額；其他舊手動工具不在同一跨產品額度內。
- 每個子工作預設最多1800秒，最多4MB CLI輸出；逾時或取消會終止並回收子程序，不記錄stderr／敏感例外內容。
- PostgreSQL Session Advisory Lock避免正常情況下多個每日Worker同時匯入；執行中會檢查鎖連線，失去鎖時取消該子工作。流程是可安全重送的at-least-once，依賴原Canonical／Revision冪等性，不能宣稱跨程序絕對exactly-once。
- SIGTERM會在目前工作完成後停止，不再啟動下一個來源，pending工作留給重啟。原匯入Run若在子程序被強制終止時仍為running，可使用各模組既有Status／Replay工具稽核與恢復；不刪除Raw或既有行情。

`daily run`只做一次，不等待15分鐘自動重跑；背景`worker`才會依持久化計數續試。CLI退出碼：0為所選工作成功／unchanged（或Status查詢成功）；2為partial／failed／blocked／busy；1為基礎設施或組態錯誤。Worker每個工作／Cycle輸出一行JSON，可用Docker logs查看。容器Healthcheck只驗證排程紀錄資料庫可讀，**不表示所有來源資料完整或最新**。

## Reviewed Evidence設定

不要把舊日期的Evidence複製改日期，也不要自行建立未核實的UUID／轉換係數。沿用各模組既有審查格式與規格，將合法已審查檔案放到`.tools/daily-evidence/`，並在`.env`設定容器路徑，例如：

```dotenv
DAILY_SYNC_AT=20:00
DAILY_SYNC_EVIDENCE_DIR=./.tools/daily-evidence
# 以下檔案須先實際審查建立；未設定時保持Raw／unresolved能力限制
DAILY_SYNC_OWNERSHIP_EVIDENCE=/evidence/ownership-reviewed.json
DAILY_SYNC_MARGIN_EVIDENCE=/evidence/margin-reviewed.json
DAILY_SYNC_LENDING_EVIDENCE=/evidence/lending-reviewed.json
DAILY_SYNC_INSTITUTIONAL_EVIDENCE=/evidence/institutional-reviewed.json
```

或在`daily run`／`daily worker`使用`--ownership-evidence`、`--margin-evidence`、`--lending-evidence`、`--institutional-evidence`。檔案以唯讀Volume掛入；不在本程式自動擴大Evidence有效日期或授權。

每個排程日建立固定的Plan／Evidence內容Hash，單一嘗試使用該Hash相符的暫存副本。新的排程日重新載入檔案；進行中的同日Cycle若檔案／參數改變，會blocked而非混用新舊證據。可還原原證據續跑，或明確用`daily run`建立新的手動Cycle；不可用重啟抹掉每日重試上限。

## Schema與驗證

新增Migration **0010_daily_sync**，只建立`daily_sync_cycles`與`daily_sync_items`，保留排程日期、Plan／Config Hash、狀態、最多3次嘗試、時間、Source Run／Raw摘要及錯誤碼；不修改DailyBar或籌碼模型。非空排程紀錄的Downgrade會拒絕，避免無聲刪除維運稽核。

```bash
# 不連外的單元測試
.tools/api-venv/bin/python -m pytest apps/api/tests/test_daily_cli.py \
  apps/api/tests/test_daily_worker.py apps/api/tests/test_daily_service.py \
  apps/api/tests/test_daily_runtime.py apps/api/tests/test_daily_freshness.py -q
# 完整離線＋PostgreSQL／Redis／Migration，建立及刪除獨立測試DB
cd apps/api
../../.tools/api-venv/bin/python scripts/run_institutional_validation.py \
  --output-dir ../../.tools/validation/daily-sync/full -- tests -m 'not network' -q --tb=short
cd ../..
docker compose --profile daily config --quiet
```

覆蓋台灣20:00／UTC切日、週末、開機補最近場次、停止訊號、跨Worker鎖／失去鎖、中斷續跑／最後工作完成視窗、最多3次重試、同日修訂、未核准來源、Evidence固定、Raw與Canonical不同步、無效／未來日期、子程序Timeout／Cancel／輸出上限，以及真實隔離PostgreSQL上的原匯入服務端到端／冪等。測試來源明示SYNTHETIC，沒有下載或寫入正式資料。

本次實際驗證：

| 檢查 | 結果 |
| --- | --- |
| 完整後端／PostgreSQL／Redis／Migration回歸 | **3,128 passed、4 network deselected**，345.80秒；1個既有Starlette warning，獨立測試DB已刪除 |
| 最後修正後全部每日同步測試 | **145 passed**，13.79秒，含122個離線案例與23個隔離PostgreSQL案例；不是與上列相加的總數 |
| 真實隔離DB端到端 | Scheduler→Executor→原DailyBarImportService→Normalizer／Raw／Run／Canonical／Revision→公開Freshness→排程紀錄；來源為明示SYNTHETIC，沒有外部HTTP |
| 冪等與恢復 | 同排程日不再呼叫匯入；同內容手動重匯仍1 Canonical／1 Revision；完成時間不因重啟／Status而改寫 |
| 七個現有正式來源Freshness Reader | 全部唯讀成功；這不是七個來源已完成新正式匯入 |
| Docker | Daily Profile config通過、最後版Worker映像建置成功；一次性容器CLI／Asia/Taipei時鐘檢查通過，未啟動排程 |
| 既有前端 | Vitest **352 passed**、Typecheck／Production Build通過；未改前端或API合約，本次未重跑Playwright |
| OpenAPI／變更檢查 | Snapshot `--check`與`git diff --check`通過 |

完整回歸後針對最後的「同一Raw Hash仍可能修復Canonical」分類調整，另將全部每日測試重新跑過；表中145項已包含該案例。修正過的其他邊界包括SIGTERM在工作間隔到達後不得啟動下一下載、最後工作提交後Cycle未提交的中斷恢復、子程序結束競態，以及鎖連線失效時取消正在執行的工作。

四個network案例沒有執行，沒有宣稱已下載新的真實行情。正式資料庫仍是`0009_lending`，`0010_daily_sync`只在隔離DB套用驗證；沒有啟動背景下載、修改既有歷史資料或停止現有服務。Artifacts在`.tools/validation/daily-sync/`，均不提交。
