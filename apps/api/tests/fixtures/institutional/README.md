# Institutional synthetic source fixtures

這三份 JSON 是本 repository 手工建立的 **synthetic 固定數值**，不是下載的交易所報表、真實交易量、正式 seed 或歷史資料。證券代號／名稱明示 `SYNTH`／`SYNTHETIC`。沒有第三方行情內容、認證、cookies 或 session token；官方欄位名稱及形狀依據 [Phase 2A 稽核](../../../../../docs/specs/phase-2a-data-sources.md)。

| 檔案                | 對應契約                            | 固定值與目的                                                                                                                                 |
| ------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `twse_modern.json`  | `twse-t86-modern-19-v1`，2026-11-06 | foreign ex net=600、trust=200、self=50、hedge=−100；dealer=−50、三大=750；foreign dealer=150 不再加入三大總計。                              |
| `twse_legacy.json`  | `twse-t86-legacy-16-v1`，2016-11-07 | 舊外資欄保留 unverified；日期只是已稽核形狀的測試標記，不能推測分類生效區間。                                                                |
| `tpex_openapi.json` | `tpex-openapi-20-v1`，民國1151106   | foreign ex net=1000、foreign dealer=−10、inclusive=990、trust=200、dealer total=100、three total=1300；hedge 僅知 sell=150，不能反推其餘欄。 |

TWSE 的 `fixture_notice` 與 TPEx 的 CompanyName 用來標明合成內容。Fixture 中的股數本來就是股，沒有乘 1,000；缺少的欄位不另造零。測試會以副本製造異常、欄位重排與缺失，不改正式畫面或資料庫。

維護方式：來源 schema、單位、scope 或分類更動時先確認官方契約與日期範圍，建立新的 contract/parser version 及 synthetic Fixture，再更新 manifest 和固定數值測試。未知格式不得透過改欄位位置或放寬歷史日期來湊成可解析。
