# Phase 1A — Market Data Domain & Database Foundation

## 範圍與架構

沿用 PostgreSQL 17、FastAPI Modular Monolith 與現有 Monorepo。僅新增 Domain、Repository、ORM、顯式 Alembic Migration 及測試；不抓取行情、不建立 API／UI、排程或 worker。

- `modules/market_data/domain/models.py`：純 Python frozen dataclasses、Enum 與輸入驗證。
- `modules/market_data/domain/ports.py`：六個同步 Repository Protocol。
- `modules/market_data/infrastructure/models.py`：SQLAlchemy 2.x typed mappings。
- `modules/market_data/infrastructure/repositories.py`：PostgreSQL Repository implementations。
- `shared/infrastructure/database.py`：Declarative Base、constraint naming、psycopg engine factory；import 不建立連線。
- `apps/api/migrations/`：獨立於目前 ORM 程式碼的 schema snapshot，API 啟動不會執行 migration 或 create_all。

Repository 接收呼叫端的 SQLAlchemy `Session`，不隱式 commit。以 `with Session(engine) as session, session.begin():` 明確管理交易；bar 的 revision 與 canonical 更新在同一交易內成功或回滾。同步 Repository 未接入 async API；未來 Application 組裝時須避免在事件迴圈直接執行同步 DB I/O。

Domain 的 `DataSource` 是供應商 metadata；Phase 0 `shared.domain.data_source.DataSource[T]` 仍是資料來源 Adapter Protocol，兩者位於不同 namespace，沒有串接任何供應商。

## Schema

| Table | Primary key | 唯一性／關聯 |
| --- | --- | --- |
| `markets` | UUID `id` | `code` 唯一；name、IANA timezone、三碼 currency |
| `instruments` | UUID `id` | `(market_id, symbol)` 唯一；FK markets；type、status、created_at、updated_at |
| `trading_calendars` | `(market_id, trade_date)` | FK markets；session_status |
| `data_sources` | UUID `id` | `code` 唯一；name、source_type |
| `ingestion_runs` | UUID `id` | FK data_sources；status、started_at、finished_at、error_summary、JSONB stats |
| `daily_bars` | `(instrument_id, trade_date)` | FK instruments 與 data_sources；每檔每天一筆 canonical |
| `daily_bar_revisions` | UUID `id` | `(instrument_id, trade_date, source_id, payload_hash)` 唯一；FK instruments 與 data_sources |

UUID 由 Domain 預設產生，不使用 symbol 作為跨市場識別碼。市場代表交易場所／市場範圍，而非國家整體；台灣上市與上櫃可以各有獨立 market，美國交易場所也可各自建立 market。相同 symbol 在不同 market 合法；同一市場同一 symbol 只能有一個 instrument。代碼比對區分大小寫，外部供應商名稱／代碼映射須在未來 Adapter 明確處理。Phase 1A 不寫入任何市場、股票或供應商 seed data。

主要索引：instrument `(market_id,status)`；calendar `trade_date`；canonical `(trade_date,instrument_id)` 與 `source_id`；revision `source_id`（其唯一索引已覆蓋 instrument/date 查詢）；ingestion `(source_id,started_at)` 與 `(status,started_at)`。所有來源與 instrument 的 FK 刪除策略為 RESTRICT，避免連帶刪除歷史。

## 日期、數值與資料約束

- `trade_date` 是市場所在地的交易日期，儲存為 PostgreSQL `DATE`；Domain 拒絕 datetime，不能把 UTC datetime 直接截斷充作交易日。跨午夜 session 的歸屬須由未來市場 Adapter 明確決定。
- 其他 timestamp 使用 timezone-aware datetime／`TIMESTAMPTZ`，預設由 Domain 產生 UTC。Market.timezone 由 ZoneInfo 驗證 IANA 名稱；資料庫同時檢查非空，但不維護另一份 IANA 時區清單。
- OHLC 是未還原權息的標準化價格，使用 `Decimal`／`NUMERIC(24,8)`，必須有限且大於零。浮點數、NaN、Infinity、超範圍或需要捨入的精度在 Domain 被拒絕；尾端多餘零不影響可表示性。
- `low <= open/close <= high` 在 Domain 與 PostgreSQL CHECK 都受限制。PostgreSQL 額外明確拒絕 numeric NaN。直接 SQL 寫入 NUMERIC 時仍遵循 PostgreSQL 的 scale 捨入行為；應透過 Domain 避免靜默捨入。
- `volume_shares` 是非負 BIGINT，單位固定為**股**，不接受 float／bool。未來來源若以張提供數值，Adapter 必須依來源定義精確轉換；本階段不猜測轉換規則。
- `turnover` 是 instrument 所屬市場貨幣的成交金額，使用非負、有限的 `NUMERIC(32,8)`；`trade_count` 是非負 BIGINT。兩者可為 NULL，代表來源未提供；NULL 和 0 明確區分。volume 必填，無交易可用 0，但不可用零價格充當缺失 OHLC。
- 使用 VARCHAR + CHECK 儲存 enum，避免把供應商字串直接當 domain status。InstrumentType：stock/etf/fund/index；InstrumentStatus：active/suspended/delisted；SessionStatus：open/closed/half_day；SourceType：exchange/vendor/file/manual。
- Calendar 是獨立參考資料，目前不要求先建立 calendar 才能保存 bar，也未實作「休市日禁止匯入」的 Application 規則。

## Canonical、revision 與 raw data

`daily_bars` 是查詢用 canonical snapshot。`daily_bar_revisions` 保存每個來源的**不同標準化觀察內容**，不是原始 HTTP／CSV payload，也不是每次重試都增加一筆的完整事件日誌。未來 raw payload retention 應建立獨立儲存與生命週期，不能混入 canonical 表。

`SqlAlchemyDailyBarRepository.upsert()` 在同一呼叫端交易中：

1. 對 UUID、日期、來源與 OHLCV 等標準化值計算 SHA-256；Decimal 10、10.0 會得到相同內容，hash 不包含 ingested_at。
2. 以 PostgreSQL `ON CONFLICT DO NOTHING` 保存 revision，重送相同來源的相同內容不新增 revision。revision.ingested_at 保存該內容第一次被持久化時的觀察時間，後續相同內容重送不改寫它。
3. canonical 以 `(instrument_id,trade_date)` 原子 upsert。較新的 ingested_at 更新內容與來源；同內容的新觀察仍推進時間，以阻擋之後送達的舊資料。相同時間保留先成功寫入的內容；較舊資料只保存在 revision。
4. 回傳目前有效的 canonical bar，而非假設輸入一定覆寫成功。

例如 A@t0 → 同內容 A@t5 → 不同內容 B@t3，canonical 保持 A@t5；revision 保存 A 與 B 兩種內容。來源不同則視為不同的來源觀察。

這是暫定的匯入時間排序規則，不是供應商 revision number／品質優先序。`ingested_at` 由可信任的匯入端提供；未來 Adapter／Application 應明確定義供應商優先級與來源修訂時間，不能把重新下載舊資料的時間誤當來源新修訂。OHLCV 內容回到先前版本時可再次成為 canonical，但相同內容不會增加 snapshot revision。

## IngestionRun

- `running`：尚無 finished_at，沒有 error_summary。
- `succeeded`：必須有 finished_at，沒有 error_summary。
- `failed`：必須有 finished_at 與非空、最多 2000 字元的錯誤摘要。
- finished_at 不可早於 started_at；Domain stats 是非負 BIGINT 整數計數字典，key 必須符合 `[A-Za-z_][A-Za-z0-9_]{0,63}`；DB CHECK 同時限制 key、範圍與整數表示，不接受 JSON 1.0 或 bool。
- Repository update 以 row lock 保護 lifecycle；來源及 started_at 不可修改，完成後不能改為其他快照，完全相同的 terminal 更新可安全重送。
- 摘要由呼叫端提供且必須先移除金鑰、連線字串及敏感 payload；本階段不會自行抓取或儲存外部例外。

## Migration 操作

從 repository 根目錄啟動：

```bash
test -f .env || cp .env.example .env
docker compose up --build --wait --wait-timeout 300
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic current
docker compose run --rm backend alembic check
```

本機安裝套件後，也可從 `apps/api` 執行：

```bash
cd apps/api
../../.venv/bin/alembic upgrade head
../../.venv/bin/alembic current
../../.venv/bin/alembic check
```

CLI 從 `apps/api` 執行時沿用既有 Settings 的 `../../.env`，連線字串不放進 alembic.ini；環境變數可覆寫。`postgresql://` 在 SQLAlchemy factory 明確切換為 psycopg driver，不影響既有 health probe。

### 安全回退

```bash
docker compose run --rm backend alembic downgrade base
```

初始 Migration 先取得資料表鎖，檢查**全部七表**是否皆空，再依 FK 反向順序移除。非空時整個降版拒絕且保留版本與資料；不會先刪幾張表再檢查。升版與降版均使用 PostgreSQL transaction。測試只在獨立 schema 執行降版。

只有已備份且明確接受刪除上述資料時，才能手動使用 `alembic -x allow_data_loss=true downgrade base`。離線 downgrade SQL 無法檢查資料，也要求這個顯式選項；一般驗收不使用它。Migration 不會刪除 database、public schema、Redis volume 或不相關的資料表。

## 測試與依賴

Runtime／test 依賴鎖於 `apps/api/requirements.lock`，Docker／CI 使用 pip `--require-hashes` 安裝；`pyproject.toml` 保留支援範圍。更新時執行：

```bash
uv pip compile --universal --extra test --generate-hashes \
  --output-file apps/api/requirements.lock apps/api/pyproject.toml
```

```bash
# 單元測試：Domain、Phase 0 health
docker compose run --rm backend python -m pytest -m 'not integration' -p no:cacheprovider
# 真實 PostgreSQL/Redis：不略過整合測試
docker compose run --rm -e RUN_INTEGRATION=1 backend python -m pytest -p no:cacheprovider
```

每個 DB 測試先建立 `test_market_data_<uuid>` schema，設定 search_path 只指向該 schema、執行 Alembic，再於 finally 只清除自身 schema。需要測試帳號有 CREATE SCHEMA 權限；不在使用者的 public 資料表 create_all／drop_all，不以 SQLite 代替 PostgreSQL。所有股票與價格範例僅為明確標記的測試資料。

## 後續需要決策

- 供應商優先序、來源 revision number、raw payload retention、還原權息序列。
- 股票代碼變更／代碼重用及跨市場同一發行人的識別與歷史。
- 來源缺失、休市／停牌日與跨日交易 session 的 Application 匯入規則。

以上未列入 Phase 1A，不會啟動 Phase 1B。

## 實際驗收結果（2026-11-04，Asia/Taipei）

| 驗證 | 結果 |
| --- | --- |
| 本機完整 Pytest，RUN_INTEGRATION=1 | 212 passed，沒有 skipped |
| 最終 Docker image 內完整 Pytest | 212 passed，沒有 skipped |
| Domain／Phase 0 單元行為 | 包含於完整測試；原有測試通過 |
| 真實 PostgreSQL Repository／Constraints／Migration／Concurrency | 包含於完整測試，使用隔離 schema |
| 全新臨時 PostgreSQL 17 database | CLI upgrade head、alembic check、downgrade base、再次 upgrade head 全部成功；測試 DB 已清除 |
| 非空資料安全回退 | 拒絕降版且保留資料／版本，測試通過 |
| Vitest | 7 passed |
| TypeScript／Next.js production build | 通過 |
| Docker Compose | build／up --wait 成功，四服務 healthy |
| 本機專案 Migration | 已套用 0001_market_data (head)，alembic check 無差異 |
| Phase 0 HTTP／Windows localhost | /health、/health/ready、首頁均 200，PostgreSQL／Redis 皆 ok |
| 本機資料隔離 | instruments、daily_bars 皆為 0 筆，測試資料未寫入正式本機表 |

一項既有 Starlette/httpx TestClient deprecation warning 仍存在，不影響測試。遠端 GitHub Actions 尚未執行，對應 CI 命令已在本機及容器驗證。沒有抓取真實資料、沒有新增 seed data，沒有 commit、push 或進入 Phase 1B。

## 檔案清單

新增（不含 package 空白 `__init__.py`）：

- `apps/api/alembic.ini`
- `apps/api/migrations/env.py`
- `apps/api/migrations/script.py.mako`
- `apps/api/migrations/versions/0001_market_data.py`
- `apps/api/requirements.lock`
- `apps/api/src/stockapp/shared/infrastructure/database.py`
- `apps/api/src/stockapp/modules/market_data/domain/models.py`
- `apps/api/src/stockapp/modules/market_data/domain/ports.py`
- `apps/api/src/stockapp/modules/market_data/infrastructure/models.py`
- `apps/api/src/stockapp/modules/market_data/infrastructure/repositories.py`
- `apps/api/tests/test_market_data_domain.py`
- `apps/api/tests/test_market_data_postgres.py`
- `docs/specs/phase-1a-database.md`

修改：`apps/api/pyproject.toml`、`apps/api/Dockerfile`、`.github/workflows/ci.yml`、`README.md`、`docs/architecture/foundation.md`、`docs/specs/phase-0-validation.md`（將 Python lock 限制標為 Phase 0 歷史狀態）。`compose.yaml` 與既有健康檢查程式維持原有實作。
