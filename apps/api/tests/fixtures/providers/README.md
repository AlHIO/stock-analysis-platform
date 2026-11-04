# Synthetic provider fixtures

These small, fixed records are synthetic test data in the official observed field
shapes. They are never imported as production market data. TWSE may return English
JSON or Chinese-header CSV; TPEx quote/catalog use different date semantics. Live
network checks use the actual official endpoints and have a separate pytest marker.

## All-securities fixtures

`securities_*.html`／`securities_*.json` 為合成測試資料，用於驗證官方 ISIN、TWSE 多表匯出、TPEx 行情與幣別公告的格式。代碼可沿用格式範例，但名稱、價格及數量僅供測試；不得透過正式匯入 CLI 使用或冒充真實行情。正式匯入由 Adapter 下載並保存真正的原始 bytes。
