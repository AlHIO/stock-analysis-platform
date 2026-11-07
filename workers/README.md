# Data Workers

歷史回補目前由可手動啟動的 Compose `history-worker` 執行，共用 API 套件的 Domain／Application／Repository，沒有另一套指標或匯入公式，也沒有排程器或微服務。

先執行 `stockapp history plan` 取得工作 UUID，再使用：

```bash
HISTORY_JOB_ID=<JOB_UUID> docker compose --profile history up -d history-worker
docker compose --profile history logs --tail=20 history-worker
docker compose run --rm backend stockapp history status --job <JOB_UUID>
```

Profile 預設不啟動，不開 HTTP port。容器不自動重啟，避免對已完成／部分失敗的日期無限重試；逐日進度保存在 PostgreSQL。停止或主機重啟後，使用同一 UUID 再啟動即可處理未完成日期。明確重試傳輸失敗日可執行 `stockapp history run --job <JOB_UUID> --retry-failed`，不要同時啟動兩個 runner。供應商 HTTP、timeout、retry、rate limit 仍位於 Infrastructure。

多段回補可明確指定順序，重啟時仍從各 job 的 PostgreSQL checkpoint 續跑：

```bash
HISTORY_JOB_IDS=<FIRST_UUID>,<SECOND_UUID>,<THIRD_UUID> docker compose --profile history up -d history-worker
# 前景執行相同清單，勿與背景 worker 同時執行
docker compose run --rm history-worker stockapp history queue --jobs <FIRST_UUID>,<SECOND_UUID>,<THIRD_UUID>
```

這是手動批次清單，不是自動排程器；最多20個不重複UUID。所有job必須先plan，啟動前驗證全部存在；不自動建立新範圍。某job尚有pending/running、下載failed或遇來源／儲存停止條件時，不會跳去下一job。只有日期都處理且沒有下載failed才接續；因歷史身分／缺價造成的partial仍保留coverage限制。Queue不隱式重試failed，需先明確使用原run的 `--retry-failed`。`HISTORY_JOB_IDS`優先於舊的單一 `HISTORY_JOB_ID`，原單一UUID指令仍可使用。

來源限制、日期與身分邊界、磁碟預算及完整操作見 [歷史回補規格](../docs/specs/taiwan-history-backfill.md)。
