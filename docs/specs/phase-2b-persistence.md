# Phase 2B-2 — Institutional Database & Persistence

日期：2026-11-08，Asia/Taipei。本次只建立法人持久化、公開證券身分查詢介面、Migration 與隔離資料庫測試。沒有新增來源 Adapter、CLI、API、排程或前端，也沒有對正式資料庫執行 Migration、操作 Docker 容器或修改歷史回補 Worker。

依據：[Phase 2A 領域草案](phase-2-domain-model.md)、[來源查證](phase-2a-data-sources.md)、[Phase 2B-1 Domain](phase-2b-institutional-domain.md)。`InstitutionalCalculator` 的數值公式、分類、Decimal 精度與缺失判定維持原規則。

## 1. 設計決策與草案差異

| 項目                                                           | 本次決策                                                                                                                                                                      |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2A 的 `chip_*` 共用 audit 草案                                 | 依本次要求建立 `institutional_*` 專用表，不提前建 ownership／margin 等產品的通用儲存。                                                                                        |
| Canonical natural key 同時作 PK 的草案                         | 實體 PK 使用 UUID，另對 `(instrument_id, trade_date, scope)` 建立 UNIQUE。完整 scope 是 JSONB 值物件，不用不可雜湊的 Python dict 當 SQLAlchemy identity key；自然唯一性不變。 |
| 「必要 leaf 與 total 必須整組存在」與 2B-1 容許 partial 的差異 | 按 2B-1 保留整份來源快照，缺欄為 NULL、缺 component 有獨立 presence。報表可以是 partial；不補零、不從舊 revision 拼欄，也不把 TPEx 缺 self／hedge 的來源判為 complete。       |
| 原 Repository `upsert` 只回 report                             | 改回 `InstitutionalWriteResult`，明示 promoted／candidate／stale／conflict／unchanged、revision／observation ID 及實際 canonical。候選資料不會被誤認為已晉升。                |
| 2A 的 provider revision 與實際能力                             | 目前僅實作 `observed-time-v1`，語意為 `observed_order_not_provider_revision`。以 raw 首次實際觀察時間排序，不宣稱供應商修訂序號、歷史當時公開狀態或 point-in-time 回測保證。  |
| Phase 2A 原 roadmap 的 2B-2 含 Adapter／CLI                    | 以本次任務為準，範圍只到 Persistence；來源契約、授權、Adapter、Normalizer、CLI 仍須另行授權。                                                                                 |

## 2. 檔案與邊界

```text
apps/api/src/stockapp/modules/
  institutional/
    domain/
      persistence.py           raw／audit／promotion 值物件、fingerprint、寫入結果
      ports.py                 repository／audit／transaction 純 Domain 契約
    infrastructure/
      models.py                五個 SQLAlchemy mappings、constraints、indexes
      repositories.py          PostgreSQL repositories、savepoint、Unit of Work
  market_data/
    public.py                  公開 identity facade
    domain/identity.py         不依賴資料庫的 DTO／Reader Protocol
    infrastructure/identity_reader.py
apps/api/migrations/versions/0005_institutional_persistence.py
apps/api/tests/
  test_institutional_persistence.py
  test_institutional_persistence_postgres.py
  test_instrument_identity.py
  test_instrument_identity_postgres.py
```

Domain 只依賴標準庫及自身模型；沒有 SQLAlchemy、FastAPI、Redis 或供應商 SDK。Institutional Infrastructure 只透過 `market_data.public` 查詢證券，不 import 其他模組的私有 Infrastructure。測試可以使用既有 catalog repositories 建立明示 synthetic 的參考資料。

重用既有 `DataSource`、`IngestionRun`、`Instrument`、`Market` UUID；沒有複製證券主檔。既有 `ingestion_runs` 與 `instruments` 僅各增加一個複合參照所需的冗餘 UNIQUE，分別為 `(id, source_id)`、`(id, market_id)`。沒有更動 DailyBar、其 revision 或 Phase 1 的晉升規則。

## 3. Schema

| 表                               | 身分與主要內容                                                                                                                                                     | 保護                                                                                                                                       |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `institutional_source_audits`    | `run_id` PK；source／market、sync 或 replay、requested_trade_date、replay_of_run_id                                                                                | 複合 FK 驗證 run 屬於該 source；replay 必須指向同 source／market／requested_trade_date（含 NULL）的另一個 audit。                          |
| `institutional_raw_payloads`     | UUID PK；run／source／market、resource／part_key、endpoint／method／parameters、完整 bytes、HTTP metadata、SHA-256、fetched_at／stored_at、replay_of_raw_id        | UNIQUE(run,resource,part)；複合 FK 綁 audit；資料庫自行核對 bytes 的 SHA-256；保留 HTTP 失敗回應。                                         |
| `institutional_report_revisions` | UUID PK；instrument／market／本地 trade_date／完整 scope；source／contract／taxonomy／unit；typed 法人快照、normalized hash、首次 raw／run                         | 相同自然鍵＋來源 profile＋內容 hash 唯一；instrument／market、raw／run／source／market 複合 FK。                                           |
| `institutional_ingestion_items`  | UUID PK；每份 raw 的扁平 row_index、locator、disposition／reason、raw_row、revision、parser／policy、publication time、observed／ingested time、promotion decision | UNIQUE(run,raw,row_index)；accepted 必須有完整 lineage；observed_at 綁定 raw.fetched_at；rejected／skipped／unresolved 不建立假 revision。 |
| `institutional_reports`          | UUID PK；唯一 `(instrument,trade_date,scope)`；所選 revision／observation、source profile、policy、observed／updated time                                          | 複合 FK 確保整份 revision 與 observation 同 instrument／日期／scope／profile；禁止跨 profile 改綁、倒退或刪除。                            |

前四張為 append-only ledgers，資料庫 trigger 拒絕 UPDATE／DELETE。Canonical 是可受控更新的選取指標；金融欄位只保存於不可變 revision，避免兩份表的金融值分歧。

### 3.1 數值與完整度

九個 category 與 Phase 2B-1 一致。每個 category 使用 `present BOOLEAN`、`buy_quantity BIGINT NULL`、`sell_quantity BIGINT NULL`、`net_quantity BIGINT NULL`、`mapping_status NULL`，不把金融值存為任意 JSON 或 float。

- `present=false` 時四個內容欄位全部為 NULL；`present=true` 時 mapping 必須是 verified 或 unverified，數量仍可缺失。
- buy／sell 非負；net 可正可負；三者已知時 `net = buy - sell`。
- 已知且 mapping 已確認的 self＋hedge、四 leaf 合計、三 subtotal 合計、foreign inclusive 均有 SQL CHECK；中間比較轉 NUMERIC，避免 BIGINT 加法溢位。
- `foreign_unspecified` 必須維持 unverified。缺欄、缺 component、實際 0 是不同內容 hash。
- 單位只接受 shares／units，不隱式把張換成股；標準化與歷史單位證據留給未來 Adapter。與目前 instrument 單位不符的 report 可留作 candidate，不能晉升。
- 報表完整度由既有 Domain 從目前所選快照推導，沒有一個可任意填寫的 `is_complete=true` 欄位。

`scope` 必須恰好包含 code、includes_block_trades、includes_odd_lots、includes_after_hours、basis。未知 flag 可保存 NULL，但不能晉升 canonical；相同 code、不同 flags 或 basis 是不同自然鍵，不會互相覆寫。

### 3.2 時間與來源追溯

`trade_date DATE` 是市場本地統計日。`source_published_on`、`source_published_at` 獨立且可為 NULL；不拿下載時間冒充發布時間。已知兩者時以 IANA timezone 驗證日期一致。

`fetched_at` 是 raw 實際觀察時間；`stored_at` 是歸檔時間；`ingested_at` 是 item 寫入時間。資料庫要求 fetched_at ≤ stored_at、observed_at ≤ ingested_at。回讀 Domain 的 first_seen_at 是相同內容所有 observation 的最早時間，last_seen_at 是該次選定 observation 的時間。

Lineage 路徑為 canonical → observation → raw → source audit → 既有 ingestion run／data source，並透過 revision 的複合 FK 核對同一 instrument／market／日期／scope／profile。raw hash 與 normalized report hash 分開，不能拿呼叫端自填 payload_hash 作為去重依據。

### 3.3 ERD

```mermaid
erDiagram
    markets ||--o{ instruments : contains
    data_sources ||--o{ ingestion_runs : owns
    ingestion_runs ||--o| institutional_source_audits : describes
    institutional_source_audits ||--o{ institutional_raw_payloads : archives
    institutional_raw_payloads ||--o{ institutional_ingestion_items : observes
    institutional_raw_payloads ||--o{ institutional_report_revisions : first_evidence
    instruments ||--o{ institutional_report_revisions : identifies
    institutional_report_revisions ||--o{ institutional_ingestion_items : repeated_observations
    institutional_report_revisions ||--o| institutional_reports : selected_snapshot
    institutional_ingestion_items ||--o| institutional_reports : selected_observation
    institutional_reports {
        uuid id PK
        uuid instrument_id FK
        date trade_date
        jsonb scope
        uuid revision_id FK
        uuid observation_id FK
        timestamptz observed_at
        string policy_version
    }
```

圖省略多欄複合 FK；ORM／Migration 才是完整 DDL。所有 lineage FK 採 RESTRICT，沒有刪除主檔而連鎖刪除 audit 的行為。

## 4. Transaction Boundary 與並發

Repository 不隱式 commit。`InstitutionalUnitOfWork.transaction()` 自己建立 Session 與外層 transaction；若使用現有 Session，則由呼叫端 commit／rollback。每次 report upsert 有內層 savepoint，revision＋accepted item＋canonical 指標在同一 transaction 中原子更新。

建議未來 Application 採以下兩個交易，尚未建立完整匯入服務：

1. 交易 A：建立既有 IngestionRun、SourceAudit，歸檔 raw 後 commit。失敗回應也可保留 raw；request metadata 必須先移除認證資訊。
2. 交易 B：normalize／Domain validate；每份報表透過 savepoint upsert。錯列 rollback 後，另記 rejected 或 unresolved item；其他有效證券是否分批 commit 由後續 Application 明確決定。
3. 後續 Application 再更新既有 IngestionRun 成功／失敗與 counters；本次沒有新建排程、IngestionRun enum 或執行資料抓取。

raw 只有在交易 A 已提交後，才能保證報表交易 B 失敗仍留下原始證據。把 raw 與報表放在同一個外層 transaction 時，外層失敗會一起 rollback，這是預期交易行為。

```mermaid
flowchart LR
    A[已有 raw bytes 與 run] --> B[交易 A: audit + raw]
    B --> C[Commit raw]
    C --> D[未來 Normalize / Domain Validate]
    D --> E[交易 B / Report savepoint]
    E --> F[PG transaction advisory lock]
    F --> G[Immutable revision + item]
    G --> H{Promotion policy}
    H -->|符合| I[整份 canonical 指標更新]
    H -->|不符合| J[保留 candidate / stale / conflict]
    I --> K[Caller commit]
    J --> K
    E -->|失敗| L[Rollback report savepoint]
    L --> M[未來 Application 記錄 rejected item]
```

同一私有 Schema／instrument／date／完整 scope 先取得 `pg_advisory_xact_lock`，再鎖定現有 canonical。空 key 初次寫入也由 PostgreSQL 協調，不靠程序內鎖。UNIQUE 與複合 FK 是第二層保護；不同程序／Worker 使用相同 PostgreSQL 資料庫與 repository 規則即可協調。鎖由 transaction／savepoint rollback 或 commit 釋放，沒有長駐記憶體鎖。

沒有 raw-body UPDATE、先刪再插 canonical 或非原子的 component 逐筆覆寫。整批多 key 交易若未採一致鎖順序，仍可能由 PostgreSQL 偵測 deadlock；未來匯入服務應採每報表短交易或固定 key 順序，並在外層交易 rollback 後重試，不在已失敗的 transaction 內盲目重送。

## 5. Idempotency 與 Revision Promotion

呼叫端必須提供明確 `PromotionPolicy(source_id, source_contract_version, taxonomy_version, quantity_unit)`。沒有「任意較晚來源都勝出」的預設。

| 情境                                                                     | 結果                                                                  |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| 相同 raw／run／row_index、相同內容及 observation metadata 重送           | 回傳原 revision／item，duplicate_observation=true，不新增 canonical。 |
| 相同 raw row 卻有不同內容、parser、publication、locator 或 raw_row       | 拒絕改寫 immutable item；需新 replay run／raw 另記觀察。              |
| 相同 normalized 內容、不同 raw                                           | 重用 revision UUID，另建 observation，保留每次 raw lineage。          |
| 未核准 source／contract／taxonomy／unit、未知 scope                      | 保留 revision＋candidate item；不建立或改寫 canonical。               |
| 已有 canonical，其他 profile 即使被新的 repository policy 指定為 primary | 保留 candidate；既有綁定不自動遷移。                                  |
| 同 profile，較新的 raw 觀察、內容不同                                    | 整份晉升；舊 revision 與晉升決定保留。                                |
| 同 profile，較新的 raw 觀察、內容相同                                    | unchanged，但更新 observation 與時間水位，防止中間較舊修正反向覆蓋。  |
| 較舊 raw                                                                 | stale；保留候選 revision／item，canonical 不變。                      |
| 同觀察時間、不同內容                                                     | conflict；保留兩份內容與原因，不推測哪份是供應商最終版。              |

例如 A@t0、A@t5、B@t3 的 canonical 仍為 A@t5；A@t0→B@t1→A@t2 只需兩份內容 revision，但有三份 observation，最後指回 A 的既有 revision 並保留 t2 lineage。

Replay 必須指向原 run／raw，同 source／market／requested_trade_date（含 NULL）且完整保留原 bytes、fetched_at、request metadata 與 headers；重播處理時間不會變成較新的行情觀察。DB trigger 也驗證此規則。Parser 變更的 replay 若與原 canonical 同時但不同內容，會記 conflict，不自動晉升。

這是觀察排序政策，不足以證明來源最新修訂；未來要加入 verified provider revision 或人工 resolution，需另有版本化規則與受審查的操作途徑，不能直接刪除 canonical 解除 profile 綁定。

## 6. 公開 Instrument Identity Reader

```python
from stockapp.modules.market_data.public import instrument_identity_reader

reader = instrument_identity_reader(session)
identity = reader.get_by_id(instrument_uuid)
resolution = reader.resolve(market_uuid, symbol, as_of=trade_date)
```

`get_by_id` 回傳不可變 DTO 或 None，包含 instrument／market UUID、market code、symbol、name、type、status、base unit、quote currency。Reader 使用呼叫端 Session、no_autoflush，不新增資料、不鎖定主檔、不 commit。僅 import public 模組不載入 SQLAlchemy 或建立連線。

目前主檔沒有完整有效日期歷史；`resolve(..., as_of=...)` 明示 `historical_unresolved / historical_registry_unavailable`，即使找到目前同碼證券也不能宣稱歷史身分已核實。不以 created_at 代替上市日。Persistence 可核對已知 instrument UUID 的 market 與目前 unit，但不代表已核實該證券在指定歷史日存續；未來 Adapter 必須處理歷史代碼／單位證據與 unresolved audit。

## 7. Migration 與安全回退

新 revision：`0005_institutional_persistence`，接續 `0004_history_backfill`。Migration 內保存凍結 DDL，不 import 可隨未來程式變動的 ORM helpers。API 啟動不會自動執行。

以下只示範**獨立測試資料庫**的操作；先設定指向該資料庫的 `DATABASE_URL`，不得直接沿用回補中的正式連線。從 repository 根目錄執行：

```bash
cd apps/api
export DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/stockapp_test'
../../.tools/api-venv/bin/alembic upgrade head
../../.tools/api-venv/bin/alembic check
# 僅當五張 institutional 表全部為空時：
../../.tools/api-venv/bin/alembic downgrade 0004_history_backfill
../../.tools/api-venv/bin/alembic upgrade head
```

Downgrade 取得五張表 ACCESS EXCLUSIVE lock，先檢查全部表，再進行任何 DROP。任一 source audit、raw、item、revision 或 canonical 非空即拒絕，沒有 allow_data_loss 捷徑；raw-only archive 也受保護。拒絕時整個 Migration transaction rollback，表、trigger、functions、舊表參照 UNIQUE 皆保留。空表可安全回到 0004，再次 upgrade。

Offline downgrade 無法確認是否含資料，因此明確拒絕。生產升級需另行核准執行時機：新增舊表 UNIQUE 與 DDL 會取資料表鎖，不能把本次隔離測試成功當成已完成正式升級。

## 8. 測試與重現

所有新增固定資料明示 synthetic，不使用真實行情冒充測試值。單元測試不需要 PostgreSQL：

```bash
RUN_INTEGRATION=0 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest apps/api/tests -m 'not integration' -q
```

整合測試先在另一個測試 PostgreSQL 資料庫設定 `DATABASE_URL`；fixture 只在該 DB 中建立隨機 `test_market_data_<uuid>` Schema、設定 search_path，完成後 DROP 該 Schema。不要指向正在回補的正式 DB。

```bash
export DATABASE_URL='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/stockapp_test'
RUN_INTEGRATION=1 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest \
  apps/api/tests/test_institutional_persistence_postgres.py \
  apps/api/tests/test_instrument_identity_postgres.py -q
RUN_INTEGRATION=1 RUN_NETWORK=0 .tools/api-venv/bin/python -m pytest apps/api/tests -m 'not network' -q
```

測試包含空 Schema upgrade／metadata drift／空表 downgrade、非空安全拒絕、直接 SQL constraints／FK、duplicate insert、獨立 Sessions 同步並發、修訂排序與 profile／scope 隔離、whole-report nulls、replay、append-only、raw-first rollback、UoW／savepoint 與公開 identity reader。

本次實際執行結果：

| 檢查                                       | 結果                                                                                                                   |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| 新增 persistence／identity 固定 unit tests | 34＋20 項，均包含於下列非整合回歸。                                                                                    |
| 全部後端非整合回歸                         | **906 passed、2 skipped、271 deselected**；2 skipped 為停用的來源 network smoke，271 為 PostgreSQL／服務 integration。 |
| Persistence＋identity＋readiness 定向整合  | **66 passed**，包含 62 persistence、3 identity、1 既有 readiness。                                                     |
| 全部後端離線回歸（含 PostgreSQL／Redis）   | **1,177 passed、2 deselected**；排除的 2 項為官方 network tests。                                                      |
| 前端 Vitest                                | **244 passed，15 files**。                                                                                             |
| TypeScript typecheck／Production Build     | 均通過；build 產生的 next-env.d.ts 變更已還原，前端無 source diff。                                                    |
| OpenAPI snapshot                           | export `--check` 通過，沒有新增或修改 routes。                                                                         |

整合執行時，臨時測試工具連到同一 PostgreSQL **17.11** instance 的 maintenance database，另建名稱隨機的 `stockapp_p2b2_test_*` 一次性資料庫，再由既有 fixture 建立私有 Schema；只對這些測試 Schema upgrade／downgrade，測試完成後移除 Schema 與一次性資料庫，並已確認本次建立的測試資料庫皆不存在。沒有連入正式應用資料庫執行查詢或 Migration。完整回歸的 pytest 參數為 `-m 'not network' -q --tb=short`。

第一次全回歸的 readiness 未通過，是臨時測試工具輸出 SQLAlchemy `postgresql+psycopg` DSN，而既有 psycopg probe 需要標準 `postgresql` DSN；只修正測試工具，未改應用程式，後續 readiness 及全回歸通過。Production Build 首次在 sandbox 中因 detached TypeScript 子程序 stdout 為空而失敗，改用允許子程序執行的本機測試環境後通過，沒有改套件或前端程式。

保留一項既有 Starlette／httpx deprecation warning，沒有為消除警告升級相依套件。沒有執行正式資料庫 Migration、來源 network tests、Docker 指令、瀏覽器 E2E 或部署。

## 9. 留待另行授權

1. 確認資料來源保存／利用權、分類契約與歷史 scope／證券身分證據，再建立 Adapter／Normalizer。
2. 建立 raw-first Application 匯入服務、IngestionRun counters／失敗狀態更新、短交易／整批鎖順序及重試政策。
3. 定義 provider revision 或人工 resolution 的可審查流程；目前 profile 綁定不自動切換。
4. 接入可信交易日曆與 history reader，再擴充 CLI／API；本次沒有新查詢 endpoint 或 OpenAPI 變更。
5. 正式資料庫的 Migration 執行另行安排；本次只提供 Migration，完成後停止。
