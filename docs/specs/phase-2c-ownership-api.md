# Phase 2C-2 — Ownership Distribution & Large Holder API

狀態：2026-11-08，Asia/Taipei。已建立canonical集保Latest／History／Distribution／Metrics及Ownership Ingestion Status，完成離線／PostgreSQL HTTP測試與正式TDCC資料驗證。沒有新Migration、來源下載、資料回補、UI或Phase 2D功能。

依據：[集保管線與Persistence](phase-2c-ownership-ingestion.md)、[Phase 2 API契約](phase-2-api-contract.md)、[Domain公式](phase-2-domain-model.md)。正式資料目前只有2330、2026-11-06一週；大戶比例可計算，週變化保留null，沒有虛構前一週。

## 1. 契約與設計決策

- 官方級距12下界是400,001，15下界是1,000,001。因此「至少400,000股／1,000,000股」切穿級距，回unsupported／null；不偷偷移動門檻或切割整桶。
- Threshold使用**基礎股數或受益單位**，`threshold_unit=shares|units`。沒有張數參數或隱含乘1000；輸入400代表400個基礎單位，不能當400張。400,001股對應超過400,000股，與至少400,000股不同。
- `/ownership/metrics`是本產品的版本化計算入口，不啟用Phase 2F泛用chip-metrics。不在API／其他功能複製公式；唯一實作是純Domain `OwnershipCalculator`。
- 現有資料沒有官方完整週報日曆的來源、版本及涵蓋證據。正式WeeklySchedule Port預設verified=false；不以週五、每隔7日或已存兩筆日期猜相鄰週。Verified schedule只由server-side Port提供，不能用query宣告。
- TDCC來源為全市場dataset，`chip-data/status?product=ownership_weekly`不造TDCC交易所；market篩選回422，source_code可用。Institutional原有market篩選繼續支援。
- 沿用已實作的產品查詢錯誤慣例：未知UUID或不支援的市場／單位回404；已存在但無報表回200空。TWSE／TPEX的shares／units證券均可查，包括有合法canonical的ETF，不套用法人stock-only限制。

## 2. Routes 與限制

| GET route | Parameters | Response |
| --- | --- | --- |
| `/api/v1/stocks/{id}/ownership` | from/to、limit=250、cursor、order=asc/desc | 完整週報分頁，每個item含整套級距 |
| `/api/v1/stocks/{id}/ownership/latest` | 無 | 最新已保存canonical；point可為null |
| `/api/v1/stocks/{id}/ownership/distribution` | as_of可選，嚴格YYYY-MM-DD | 指定統計日的完整分布；省略用latest，指定但無資料不回填其他日 |
| `/api/v1/stocks/{id}/ownership/metrics` | from/to、threshold=400001、threshold_unit=shares、limit/cursor/order | 每個已保存週報的ratio與change_pp |
| `/api/v1/chip-data/status` | product=ownership_weekly、source_code、limit≤100、cursor | global TDCC能力、run／audit／canonical coverage |

`{id}`沿用Instrument UUID，不能直接傳2330。市場本地日期inclusive；預設to=當地今天，from=to−365日，最多3660日曆日。拒絕未來、倒序、compact／含時間日期；limit必須1–1000，metrics同樣分頁，不會無限制載入全部歷史。Threshold嚴格正BIGINT整數，拒絕bool、float／scientific字串、前導0及越界。

Keyset cursor包含查詢signature，綁定日期、排序、limit及metrics門檻／單位；換參數須從第一頁開始。Cursor不是授權簽章。每頁獨立read_at／DB snapshot，不宣稱跨請求revision不變。分頁不拆同一份週報的buckets；後續頁coverage.truncated仍標true，不能把單頁當完整範圍。

## 3. Distribution／Lineage／Freshness

實際Pydantic contract及nullable／enum以`apps/api/openapi.json`為準。

Point包含：

- as_of_date、frequency=1w、quantity_unit、denominator=depository_inventory、bucket_scheme_version、adjustment_value_convention。
- source_total_holders／source_total_quantity，以及每個bucket的code／kind／label／inclusive bounds、holder_count／quantity／reported_percent、missing flag。
- regular／adjustment／total分開；缺bucket只給明確missing／null，不取其他日期拼接。正式canonical reader不公開candidate。
- timing分開統計日、source_published_on／at、first_seen、last_seen、ingested；timestamp必須aware並以UTC輸出，發布未知保留null。
- provenance包含source{id,code,name}、revision／run／raw UUID、row locator、parser／source contract／promotion版本、normalized hash、identity evidence。
- quality有status、Domain completeness、missing bucket codes、unavailable reasons與來源percent residual flags。99.94%殘差不自動正規化，也不把已核實報表當不完整。

所有財務quantity／holder count／bounds用精確整數字串，reported percent／衍生percent用Decimal字串；null不等於`"0"`。分頁、row count等metadata才是JSON integer，不改Phase 1既有volume整數契約。

Coverage的stored_from/to只是本平台保存端點，預設completeness=not_verified；不宣稱中間齊全。Metrics只列已核實schedule證明的missing dates，不從當前分頁推測全歷史缺口。Freshness提供最新保存統計日、source publication、last_seen及age_calendar_days；無已核實發布SLA時assessment=not_verified，不猜stale，is_realtime=false。

## 4. Financial Calculation Contract

### Ratio v1

```text
selected = regular buckets whose lower_inclusive >= threshold
ratio_percent = 100 × sum(selected.quantity) / same report's official source_total_quantity
```

前提：threshold必須恰為scheme的regular下界，報表及單位可核實、必要buckets完整、來源分母非null且>0。不可加上adjustment或total再算一次，不用reported_percent加總替代股數公式，也不改用發行股本等未知分母。

- 門檻切穿級距／單位不相容：unsupported，value=null。
- 缺資料、零／缺分母、不完整報表：unavailable，value=null。
- 全部regular數量可能因差異調整而大於官方total；若選中numerator>denominator，回unavailable／`numerator_exceeds_official_denominator`，不截成100%或產生誤導比例。
- numerator_quantity／denominator_quantity、selected_bucket_codes、formula、threshold／unit、input_refs、algorithm_version及precision隨結果提供。

### Weekly Change v1

```text
change_pp = current_ratio - previous_ratio
unit = percentage_points
```

不是`(current_ratio / previous_ratio - 1) × 100`。只有官方schedule確認兩個統計日相鄰，且instrument／market／source／contract／scheme definitions／調整慣例／unit／denominator相容時才計算。前一週缺失不能跳過，最近兩筆不必然相鄰；對不同口徑不合併。即使verified schedule的兩筆相鄰，若ISO統計週身分不連續，仍回not_verified／non_consecutive_reporting_weeks，不把多週變動稱為單週；這不要求實際統計日恰好相隔7日，假期移動到週三／週四仍可驗證。

Calculator可回complete、unsupported、unavailable、not_verified、missing_data、incompatible_data與原因。缺已確認週列missing_dates；無verified schedule不輸出週差值。若查詢從某週開始，可最多多讀一份嚴格早於第一個item的canonical當previous input；其日期可在from之前，只作input_ref，不偽裝成查詢範圍內新觀察。暖機schedule範圍仍有3660日上限。

每個結果保留兩份輸入的日期、revision、source、contract／scheme、單位、發布／觀察時間。wrapper的source_contract_versions列目前分頁報表的版本，查詢範圍外previous的版本完整保留在input_refs。正確統計日不表示已知當時發布時間，不提供vintage回測保證。

### 精度

算法版本`ownership-metrics-1.0.0`。Decimal local precision=50，公開ratio輸出12位小數、ROUND_HALF_EVEN；weekly change減的是**相同公開12位ratio**，因此PP精確等於回傳current_ratio−previous_ratio。例66.666666666667−33.333333333333=33.333333333334 pp；此規則有固定數值與rounding測試，不依賴全域Decimal context。

## 5. PostgreSQL、效能與安全

Query Repository只讀canonical的選定revision／observation與typed buckets，不解析raw或在GET時晉升candidate。每頁批次載入header、buckets、first_seen、scheme／definitions，最多5個資料SELECT，加每個不同source一次可快取metadata查詢，避免逐報表／逐級距N+1。現有PK`(instrument_id,as_of_date)`支援日期範圍、排序、previous及keyset；不需新增索引或Migration。

每個request使用一個`REPEATABLE READ READ ONLY`Session，statement_timeout=10秒、lock_timeout=3秒。Source／revision／distribution／metrics／freshness在同一snapshot讀取；其他交易提交修訂不會混進本次回應。跨模組只經`market_data.public`。Dataset status在root composition透過各模組public query factory組裝，保留Institutional既有路徑及operationId，不讓API直接存取其他模組私有Infrastructure。

GET不觸發TDCC／TWSE／TPEx網路請求，無DB寫入或自動Migration。Status可讀raw的count／fetched_at metadata，但不讀body或diagnostic raw_rows。錯誤：404未知／不支援；422無效日期／limit／threshold／cursor／filter；503 DB或stored data不可用，保留既有detail.code/message/errors envelope，不暴露SQL／DSN／密鑰。

## 6. 實際執行的測試

| 項目 | 結果 |
| --- | --- |
| 全後端／PostgreSQL／Migration／Phase1／Institutional／Ownership regression | **1918 passed，4 network deselected**，209.15秒；獨立臨時DB＋逐例UUID schema，完成後清除 |
| 新Domain numerical tests | **63 passed**，exact boundary／Decimal／PP／缺週／跨年／口徑／精度 |
| 新API unit/contract tests | **42 passed**，真QueryService經HTTP/Pydantic，含共享status及安全503 |
| 新PostgreSQL HTTP tests | **15 passed**，canonical lineage／null publication／revision／batch reads／snapshot／GET無provider I/O |
| 新Application query tests | **16 passed**，分頁PP一致、query外previous input、缺週、閾值／單位及空資料 |
| Vitest | **244 passed，15 files** |
| TypeScript／Next.js Build | typecheck／Production Build通過；next-env產生檔已還原，無前端source改動 |
| OpenAPI | runtime生成snapshot，export --check通過 |
| 現有網站瀏覽器回歸 | Playwright桌面真實日／週／月圖與手機查詢／尺寸：**2 passed** |
| Docker／Migration | Backend build／替換與health正常；current=head=`0007_ownership`，alembic check無差異；沒有執行upgrade或新增Migration |

4個network tests未在回歸執行；本階段沒有下載或匯入新行情。保留既有Starlette/httpx TestClient deprecation warning，沒有無關套件升級。舊Institutional測試僅更新共享status的dependency override，金融斷言未放寬。

可重跑：

```bash
.tools/api-venv/bin/python -m pytest apps/api/tests/test_ownership_calculations.py \
  apps/api/tests/test_ownership_api.py apps/api/tests/test_ownership_query_service.py -q
.tools/api-venv/bin/python apps/api/scripts/run_institutional_validation.py -- tests -m 'not network' -q --tb=short
.tools/api-venv/bin/python apps/api/scripts/export_openapi.py --check
```

隔離測試使用明示synthetic報表；verified schedule只由測試server-side fixture提供，不當作官方歷史覆蓋。PG跨年固定樣本50%→60%=**10 percentage points**，HTTP固定樣本55%→70%=**15 percentage points**；另驗證非固定週五、缺已確認週不跳接、尚未核實schedule回null，以及ETF的units不可用shares門檻代算。

## 7. 正式資料庫與真實API驗收

使用已保存的2330／2026-11-06 TDCC canonical，對Latest、History、Distribution、Metrics及Ownership Status實際發HTTP GET；DB驗證另使用read-only snapshot逐級距比對，沒有寫入測試資料。

- Latest／History／指定as_of Distribution一致，17個buckets的人數、quantity、Decimal percent及revision與PostgreSQL完全相同。
- source_total_holders=`3259221`，source_total_quantity=`25932370067`；調整人數3、調整股數20000獨立保存；來源發布時間null，percent residual flag仍存在。
- 獨立SQL取selected regular buckets，再用Decimal50／HALF_EVEN12公式核對API：

| Inclusive threshold | Selected regular codes | Numerator quantity | Official denominator | API ratio_percent |
| --- | --- | ---: | ---: | ---: |
| 400001 shares | 12／13／14／15 | 22563646397 | 25932370067 | **87.009580453709** |
| 1000001 shares | 15 | 21846818147 | 25932370067 | **84.245358563662** |

`threshold=400000`及`1000000`實際回unsupported／null。兩個可算門檻的change_pp均為null／not_verified，原因包含`weekly_schedule_not_verified`與`previous_report_missing`；目前沒有上一週或官方完整週報日曆，沒有把未知值變0。

正式保存範圍仍只有**1週、1檔2330**：2026-11-06～2026-11-06，canonical=1、revision=1、bucket_values=17、raw=2、item audits=8230、jobs=2。這些是Phase 2C-1既有資料，不是本次新匯入或十年完整集保資料。Status反映accepted／unresolved及partial run，不將8230個觀察audit當作獨特週報數。

本機驗收摘要在gitignored `.tools/validation/phase-2c2/api-validation.json`，含真實provenance／timing及公式對照；正式raw仍由Phase 2C-1保存於DB。本次GET前後ownership counts／範圍完全一致，canonical=1、revision=1、buckets=17、raw=2、items=8230、jobs=2；未改動資料。

## 8. 變更檔案與未完成範圍

- `ownership/domain/calculations.py`、`query_results.py`、`queries.py`：唯一計算引擎、週報日曆、typed results與read Port。
- `ownership/application/query_service.py`：bounded pagination、distribution、metrics、coverage／freshness及來源狀態。
- `ownership/infrastructure/query_repository.py`：canonical batch projection及metadata。
- `ownership/api/{__init__,dependencies,routes,schemas}.py`、`ownership/public.py`：HTTP契約及組裝邊界。
- `stockapp/chip_status.py`、`institutional/public.py`、既有Institutional route／main：共用status dispatch與路由註冊。
- 四個新ownership測試檔、三個既有Institutional測試helper、`apps/api/openapi.json`、本Spec／共用Contract／README／roadmap。

以上模組路徑均在`apps/api/src/stockapp/modules/`，root chip_status／main在`apps/api/src/stockapp/`。

正式週變化數值仍需更多真正已匯入的相鄰週報與有來源／版本／涵蓋範圍的TDCC schedule。沒有自動下載歷史、補零、沿用上一週作新觀察、公開raw、前端／個人大戶交易辨識或自動交易。Phase 2D未開始；沒有commit／push。
