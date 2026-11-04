# 開發規範

## 範圍與工作方式

- 修改前先用文字列出執行計畫，確認需求所屬 Phase；未經需求授權不得進入下一階段。
- 先讀 README、相關模組與架構文件；保留現有未提交變更。
- 採 Modular Monolith + Data Workers。不要提前拆微服務或導入 Kubernetes。
- 變更完成必須執行可用測試，回報執行指令、結果、略過項目及環境限制；不得把未執行當作通過。

## 架構

- 業務模組位於 `apps/api/src/stockapp/modules/<module>/`，以 api、application、domain、infrastructure 分層。
- Domain 不依賴 FastAPI、資料庫、Redis 或外部供應商 SDK。Application 依賴 domain ports；infrastructure 實作 ports。
- API 負責輸入輸出與依賴組裝，業務規則放 application/domain；跨模組經明確介面，不直接存取另一模組資料表。
- 所有資料來源經 Adapter 抽象化。供應商細節、認證、重試及 rate limit 放 infrastructure；worker 共用 domain/application。
- 不虛構股票行情作為真實資料。測試假資料須明確標示且只能用於測試。
- 新資料表透過可審查的 migration，避免啟動時隱式變更 schema。
- 前端於 `apps/web/src`；伺服器環境變數不可意外改成 `NEXT_PUBLIC_*`。

## 安全與品質

- `.env`、金鑰、連線密碼不可提交；新增設定須更新 `.env.example` 與 README，僅提供開發佔位值。
- 網路 I/O 設定 timeout；API 錯誤不可洩漏機密或內部連線資訊。
- 依賴變更同步更新 lockfile；不要未經需求授權進行無關升級。
- 針對有意義的行為新增 Pytest/Vitest 測試，包含外部服務失敗情境；外部整合測試須與單元測試分開。
- 基本檢查：`python -m pytest apps/api/tests -m 'not integration'`（使用專案 venv）、`npm test`、`npm run typecheck`、`npm run build`。
- Compose／連線變更還須執行 `docker compose config --quiet`、啟動健康檢查及真實 PostgreSQL／Redis 整合測試；若環境不支援，明確記錄。
- 更新受影響文件，完成後回報變更、驗證及剩餘事項。不要自行 commit、push、部署或啟動下一 Phase。
