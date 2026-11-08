# Phase 2B-4 — Institutional Data Ingestion CLI

日期：2026-11-08，Asia/Taipei。本次實作手動 CLI、raw-first Application Service、逐列交易、持久 checkpoint 及 PostgreSQL／離線／核准來源驗證。不建立法人 API、UI、排程或長期同步服務；沒有對正式資料庫執行 Migration，也沒有操作 Docker 或 Phase 1 history-worker。

依據：[Domain](phase-2b-institutional-domain.md)、[Persistence](phase-2b-persistence.md)、[Adapter](phase-2b-adapters.md)。`InstitutionalCalculator`、DailyBar 與既有 revision promotion policy 維持原規則。

## 1. 能力與限制

| 來源            | 本次 CLI 能力                                                               | 保留的限制                                                                                                                    |
| --------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| TPEx I2 OpenAPI | 明確執行 sync latest 時啟用已確認 OGDL 資料集；支援已歸檔 raw 的離線 Replay | 不接受日期查詢；self／hedge 缺欄且 scope 未核實。已確認身分的有效資料可保存 revision／candidate，不能冒充完整 canonical。     |
| TWSE I1 T86     | CLI 語法存在，來源 gate 明確拒絕並記錄失敗原因                              | 使用權待確認；sync／CLI replay 均不能以布林開關解除。Adapter 的 synthetic offline Parser 測試仍可執行，但不等於開放正式來源。 |
| 日期匯入        | `sync date` 明確接受 ISO 日期並檢查來源能力                                 | 目前沒有已核准且可由 CLI 執行歷史下載的來源：TPEx latest-only，TWSE pending。不能把命令存在說成已具備完整歷史法人匯入。       |
| 證券身分        | 重用已存在 Instrument UUID，支援明確的 dated evidence 檔                    | 今日 registry match／created_at 不等於歷史身分證據。沒有有效證據的列保留為 unresolved，不新增假證券主檔。                     |

TPEx 授權證據沿用 [官方資料集11856](https://data.gov.tw/dataset/11856)與 [2A 稽核](phase-2a-data-sources.md)，沒有擴張至 I3 AL 歷史匯出的使用權。`sync latest` 是使用者明確要求執行的一次下載；import 模組、status、replay 或網站請求不會下載資料。

## 2. 修改檔案與邊界

```text
apps/api/src/stockapp/
  cli.py                                  增量註冊 institutional 子命令
  modules/institutional/
    domain/ingestion.py                   Job、SourceSpec、Store／Provider Ports、counters
    domain/identity_snapshot.py           不依賴DB的凍結身分 resolver
    application/ingestion.py              手動 sync／replay／resume 流程
    infrastructure/
      operations.py                       CLI composition、核准來源與 bounded HTTP 組裝
      identity.py                         evidence 檔驗證與 public reader snapshot
      ingestion_store.py                  raw-first、短交易、checkpoint、status、PG locks
      ingestion_models.py                 一張 operational checkpoint 表
      twse_parser.py / tpex_parser.py      非法NUL／surrogate文字逐列拒絕修正
  modules/market_data/
    public.py                             additive runtime facade
    domain/runtime.py                     純 DTO／Protocol
    infrastructure/runtime_registry.py    本模組擁有的 catalog／run lifecycle 實作
apps/api/migrations/versions/0006_institutional_ingestion_jobs.py
apps/api/tests/
  test_institutional_cli.py
  test_institutional_identity_evidence.py
  test_institutional_ingestion_postgres.py
  test_institutional_ingestion_network.py
```

Application 只依賴自己的 Domain 與 Normalizer；沒有 SQLAlchemy、httpx 或供應商 import。Institutional Infrastructure 透過 `market_data.public.market_data_runtime(session)` 重用 Market／DataSource／IngestionRun，不直接存取其他模組的私有資料表。runtime facade 不隱式 commit，因而可在同一 Session 把 parent run counters 與 checkpoint／report 一起提交。

法人來源使用專屬 `TWSE_INSTITUTIONAL_T86`／`TPEX_INSTITUTIONAL_OPENAPI` DataSource code。市場按既有 TWSE／TPEX code 重用；來源按 code 冪等註冊，不覆寫既有 metadata。沒有複製 Instrument 主檔。

## 3. CLI

命令依既有 `stockapp` argparse 風格，輸出 JSON。`--help` 不讀取 Settings，也不建立 engine。

```bash
stockapp institutional sync latest --market tpex
stockapp institutional sync latest --market tpex --identity-evidence /path/to/reviewed-evidence.json

# 語法存在；目前 TPEx 因 latest-only 拒絕，TWSE 因使用權 pending 拒絕
stockapp institutional sync date --market tpex --date 2026-11-06
stockapp institutional sync date --market twse --date 2026-11-06

# 使用 status 回傳的 selected_raw_id；建立新的 replay run，不下載
stockapp institutional replay --raw <RAW_UUID>
# 可對新 replay 提供更新且已確認的身分證據，保留原run的舊拒絕紀錄
stockapp institutional replay --raw <RAW_UUID> --identity-evidence /path/to/reviewed-evidence.json

# 只續跑尚未結束的既有run，使用已凍結的證據與版本；不下載
stockapp institutional replay --resume <RUN_UUID>

stockapp institutional status --limit 20
stockapp institutional status --id <RUN_UUID>
```

sync 預設 market=tpex，不提供會隱式混合未核准來源的 all 選項。日期要求嚴格 YYYY-MM-DD；拒絕民國、compact date、無效日。replay 的 `--raw`／`--resume` 互斥，resume 不可帶新 evidence 檔。

退出碼：0 為 ingestion succeeded 或成功執行 status 查詢；2 為 partial／failed／blocked、查無指定 run、能力或資料契約錯誤；1 為連線／未預期基礎設施錯誤；130 為中斷。status 查詢成功不表示被查 run 成功。

### 3.1 Dated identity evidence

可選 JSON 檔格式如下；placeholder 必須換成真實存在的 UUID 與已核實的有效日期，不可為了增加 accepted 數而自行填日期：

```json
{
  "version": "institutional-identity-evidence-v1",
  "synthetic": false,
  "entries": [
    {
      "instrument_id": "<existing Instrument UUID>",
      "market_id": "<existing TPEX Market UUID>",
      "symbol": "<verified security symbol>",
      "quantity_unit": "shares",
      "valid_from": "<verified YYYY-MM-DD>",
      "valid_through": "<verified YYYY-MM-DD>",
      "evidence_version": "<traceable evidence version>"
    }
  ]
}
```

最多5,000,000 bytes／20,000 entries；拒絕重複 JSON key、重複 market／symbol、未知欄位、錯誤 UUID／range／unit、未認識版本與 `synthetic=true` 的 CLI 輸入檔。這是明確的可信證據輸入，程式不會因檔名存在而代替呼叫端核實歷史資料。

plan 建立時，public identity reader 核對 UUID、market、symbol、目前基礎單位，再按交易日核對 evidence range。結果連同 evidence_version 凍結；沒有證據標 historical_identity_unresolved，根本不存在的證券標 unknown_instrument。

沒有提供新 evidence 的 `replay --raw` 會沿用原 job 的 evidence；舊版 archive 若沒有 job 則維持空證據，不推測。resume 一律使用原 plan snapshot，不重新讀外部檔。若要重處理已拒絕列，另建 replay run，不改寫舊 item ledger。

## 4. Pipeline 與 Transaction Boundary

```mermaid
flowchart LR
    A[建立Run / SourceAudit / Job] --> B[核對來源權限與capability]
    B --> C[有界HTTP / 每次完整回應callback]
    C --> D[Raw Archive短交易 Commit]
    D --> E[Schema / 官方日期 / Parser]
    E --> F[凍結plan與identity snapshot]
    F --> G[Normalizer / Existing Domain Validation]
    G --> H{逐列結果}
    H -->|有效| I[Revision + Promotion + Item + Cursor + Counters]
    H -->|異常| J[Rejected / Unresolved Item + Cursor + Counters]
    I --> K[同列短交易 Commit]
    J --> K
    K --> L[最終Run狀態與Audit Report]
```

建立 job、raw 歸檔、plan、每筆報表、結束狀態採明確短交易；HTTP 期間不持有開啟的資料庫交易。每次 HTTP 完整回應（包括403／429／5xx）先歸檔，retry 前也會 commit；每個 attempt 有獨立 raw UUID／part_key。超限截斷 bytes 不會假裝成完整 raw。

每列的 revision、accepted item、canonical promotion、next_row_index 與 IngestionRun stats 同一 transaction 提交。若報表交易失敗，該列資料與游標一起 rollback；可辨識的資料／constraint 錯誤再以獨立 rejected item 記錄，其他證券繼續。未預期 DB／程序錯誤保持 running checkpoint，讓使用者在原因排除後 resume；不吞掉錯誤並假稱整批成功。

Parser／日期／schema 的 batch error 保存於 run.error_summary，raw 保留；不能解析的 batch 不創造假 row。個別資料錯誤以 row_index／locator／reason 追溯。原始 bytes 始終保留；無法安全寫入 PostgreSQL JSONB 的 diagnostic raw_row 可為 NULL，例如含NUL／surrogate，避免診斷欄位再中斷有效股票。

## 5. Checkpoint Schema 與 Resume

新增 `institutional_ingestion_jobs` 一張 operational 表，PK=`run_id`，以完整 run／source／market FK 指向 source audit，selected_raw_id 與同一 run／source／market 的 raw 複合 FK 綁定。金融值仍只在既有 revisions，沒有塞進 job JSON。

主要欄位：provider、manifest version／fingerprint、config_json、evidence_json、phase、selected raw、官方 trade_date、plan_json／fingerprint、next_row_index、created／updated time。

```mermaid
stateDiagram-v2
    [*] --> fetching: sync
    [*] --> archived: clone existing raw for replay
    fetching --> archived: successful raw selected
    archived --> processing: plan frozen
    processing --> completed: all rows checkpointed
    fetching --> failed: source/capability failure
    archived --> failed: schema/plan failure
    processing --> failed: fatal contract failure
```

資料庫 trigger 保護 source config／evidence／manifest／已選 raw／已建立 plan 的不可變性；游標只能逐次＋1，不能倒退或跳過列。terminal checkpoint 不可修改、刪除；空表可 downgrade，非空 checkpoint 明確拒絕。

plan 包含 raw ID／SHA-256、官方日期、contract／parser／normalizer version、scope／unit、row_count、manifest fingerprint、凍結 identity snapshot 與已知 publication metadata。resume 重新解碼原 raw，核對同一份 metadata／plan hash，只處理 `row_index >= next_row_index` 的列。

中斷情境：

- raw 已歸檔、尚未選取：resume 可恢復該 run 的成功 HTTP200 archive，不再下載。
- 逐列處理途中中斷：已 commit 的 row／cursor 保留，從下一列繼續。
- 沒有成功 archive：resume 明確失敗，不偷偷重送 HTTP。需要另執行 sync 建新 run。
- terminal failed／partial：不改回 running；resume 只回原狀態。若有適當 archive，可另建 raw replay。
- manifest／parser／normalizer／plan 不相容：拒絕續接，不用新規則悄悄只重算後半批。

## 6. 並發、重試與晉升

同 job 的 resume 使用 PostgreSQL session advisory try-lock，不能只靠程序鎖；不同 job 的 report 仍經既有 instrument／date／完整 scope transaction lock、UNIQUE 與複合 FK 保護。測試使用不同 Sessions／threads，驗證同 raw 並發 Replay 只有一個內容 revision 與 canonical。

Institutional HTTP 工作另以同資料庫／Schema／host advisory lock 協調，拿不到鎖即回可追溯 busy failure；取得後在 fetch 前後各保留預設5秒間距。HTTP 內部仍使用既有 host limiter、最多3次、有限timeout、Retry-After與20MB界線。這不是官方quota，也**不宣稱已協調其他資料庫、Phase 1程序或外部程式的流量**；本次未修改 history-worker。

Canonical promotion 完全沿用 `observed-time-v1`：同 source profile、同 scope、已核實 scope／單位才可選取；不同來源／taxonomy／contract／scope 不合併。partial data 可保存，但不能把 candidate 當 canonical。相同內容重送可重用 revision，較新相同觀察可更新 freshness 水位；舊 raw Replay 保留原 fetched_at，不以 replay time 提高優先序。

若原 accepted item 已有 source_published_on／source_published_at，Replay plan 保留並恢復；未知仍為 None。first_seen_at 由原 fetched_at 與既有 Repository 的 revision observations 最早值維持，不改成執行日期或重播日期。

## 7. Status 與數量語意

`status` 回 run ID、provider、官方／requested date、phase、游標、stats、error summary、raw metadata、row issue／promotion reason preview、來源限制與 attribution／license。issue／decision preview 各最多100筆，精確數量以完整 counters 為準，不回傳 raw body 或連線資訊。

| 欄位                                            | 意義                                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------------- |
| received／processed                             | 已解析來源列數／已原子提交的處理列數；成功結束時相等。                      |
| accepted                                        | 成功保存 typed report／observation 的列數，不代表已晉升 canonical。         |
| complete／partial                               | accepted 中既有 Domain completeness=complete／其餘狀態的列數。              |
| rejected／unresolved                            | 不合法資料／無法核實身分或單位的列數，與0不同。                             |
| promoted／unchanged／candidate／stale／conflict | 既有 Repository 的逐列 promotion decision。                                 |
| revisions_created                               | 新的不同內容快照數，重複內容不重建。                                        |
| source_failures                                 | 造成該 run 結束的來源／schema／能力錯誤次數；HTTP各次回應另見raw metadata。 |

對外 status：全部資料可用且沒有能力／品質缺口為 succeeded；有效報表有保存但存在 partial／candidate／conflict／rejected／unresolved 為 partial；fatal failure 或無任何 accepted 為 failed。未完成為 running。

沿用既有 IngestionRun 的 running／succeeded／failed enum；partial 對外明示 status=partial，底層 run_status=failed 加 counters／摘要，不新增不相容 enum。phase=completed 只代表全部列已處理，不等於資料完整。

## 8. Migration 與操作環境

新 migration 檔名 `0006_institutional_ingestion_jobs.py`，revision ID=`0006_institutional_jobs`，接續0005；短ID符合既有 Alembic version 欄位長度。Migration 保存凍結 DDL，不讀可隨程式變動的模型 helper。

本次只在一次性 PostgreSQL 測試資料庫／私有 Schema 執行 upgrade／downgrade，**正式資料庫未套用0006**。使用者操作前應另行安排核准升級時機；API／CLI 啟動不會自動修改 schema。

以下只用於已建立的獨立測試資料庫，請替換為自己的測試連線，不要沿用回補中的正式 DATABASE_URL：

```bash
export DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/stockapp_test'
cd apps/api
../../.tools/api-venv/bin/alembic upgrade head
../../.tools/api-venv/bin/alembic check
# 僅當 operational checkpoint 表全部為空：
../../.tools/api-venv/bin/alembic downgrade 0005_institutional_persistence
../../.tools/api-venv/bin/alembic upgrade head
cd ../..
.tools/api-venv/bin/stockapp institutional status
```

downgrade 先鎖 checkpoint 表、檢查是否有資料，非空拒絕；offline downgrade 無法檢查資料也拒絕。沒有 allow_data_loss 或隱式刪除 archive／revision 的路徑。

## 9. 測試與實際結果

新增CLI、identity evidence與完整 PostgreSQL pipeline 測試；數值 Fixture／Resolver／測試來源明示 synthetic。核准來源 live test 與離線測試分開，沒有用假證券身分接受真實行情。

```bash
RUN_INTEGRATION=0 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration' -q

# DATABASE_URL 必須是另一個獨立測試資料庫；fixture再建立隨機私有Schema
RUN_INTEGRATION=1 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_institutional_ingestion_postgres.py -q
RUN_INTEGRATION=1 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest apps/api/tests -m 'not network' -q
```

| 驗證                                          | 結果                                                                                                                                                                             |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 全部離線後端（含 PostgreSQL／readiness）      | **1,487 passed、3 deselected**；排除3項network tests。                                                                                                                           |
| 新增 PostgreSQL pipeline 案例                 | 包含25項於上列回歸，涵蓋offline replay、冪等、revision、partial、source errors、callback raw、並發、rollback、checkpoint recovery、publication與Unicode列隔離。                  |
| 後端非整合回歸                                | **1,191 passed、2 skipped、297 deselected**；2 skipped是停用的其他network smoke，297是integration。                                                                              |
| 核准 TPEx 真實來源隔離匯入                    | **1 passed**。真實 raw 歸檔、Schema／官方日期解析及unresolved audit完成；沒有主檔／dated evidence時不建立假身分，accepted與canonical promotion皆0，不宣稱真實canonical驗證完成。 |
| 前端Vitest                                    | **244 passed，15 files**。                                                                                                                                                       |
| Typecheck／Production Build／OpenAPI snapshot | 均通過；build產生的next-env.d.ts變更已還原，前端無source diff，沒有法人API或既有OpenAPI變更。                                                                                    |

整合執行工具另建隨機 `stockapp_p2b4_test_*` 一次性資料庫，fixture 在其中建立 `test_market_data_<uuid>` 私有 Schema；測試結束後移除 Schema 與資料庫。沒有連入正式應用資料庫執行查詢／Migration／寫入。

Live test 為 `test_institutional_ingestion_network.py`，需同時設定 RUN_INTEGRATION=1、RUN_NETWORK=1、RUN_INSTITUTIONAL_LIVE=1，並驗證 database 名稱為本次專用 temporary prefix 及 private Schema。它只下載核准 I2，不存取 TWSE；不繞過403或存取限制。既有其他兩項network smoke本次未執行。

保留既有 Starlette／httpx deprecation warning；沒有升級套件或修改前端程式、正式資料、worker、Compose，也未 commit／push／部署。

## 10. 未完成的外部依賴與停止點

- TWSE 使用權、歷史分類邊界與更多已核實日期；未取得前繼續停用。
- TPEx 完整 self／hedge 及交易範圍證據；目前仍是 partial／candidate 能力。
- 真實 dated identity evidence，不能用當前主檔冒充歷史。CLI可保存原始資料與拒絕原因，不因此宣稱已收齊法人canonical。
- 正式資料庫0006升級另行核准執行。本階段只交付Migration與隔離驗證。

到 Phase 2B-4 結束停止，不自行開發法人 API／UI 或啟動長期同步。
