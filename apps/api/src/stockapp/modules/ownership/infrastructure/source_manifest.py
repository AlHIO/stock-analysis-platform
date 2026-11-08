"""Reviewed TDCC O1 source: latest CSV only, independent of HTML history."""
from dataclasses import dataclass
from datetime import date

TDCC_ENDPOINT = "https://opendata.tdcc.com.tw/getOD.ashx"
TDCC_PARAMETERS = {"id": "1-5"}
TDCC_CONTRACT_VERSION = "tdcc-csv-6-v1"
PARSER_VERSION = "1.0.0"
TDCC_FIELDS = ("資料日期", "證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%")


@dataclass(frozen=True, kw_only=True)
class OwnershipSourceManifest:
    code: str = "TDCC_OWNERSHIP_CSV"
    name: str = "臺灣集中保管結算所：集保戶股權分散表"
    endpoint: str = TDCC_ENDPOINT
    method: str = "GET"
    request_parameters: tuple[tuple[str, str], ...] = (("id", "1-5"),)
    version: str = "tdcc-ownership-sources-1.0.0"
    contract_version: str = TDCC_CONTRACT_VERSION
    parser_version: str = PARSER_VERSION
    authorization: str = "ogdl_dataset_documented"
    license: str = "OGDL-1.0"
    attribution: str = "臺灣集中保管結算所：集保戶股權分散表；依政府資料開放授權條款第1版利用"
    capability: str = "latest_only"
    frequency: str = "1w"
    timezone: str = "Asia/Taipei"
    denominator: str = "depository_inventory"
    default_network_enabled: bool = False
    official_rate_limit: str | None = None
    source_published_at: str | None = None
    verified_on: date = date(2026, 11, 8)
    evidence_urls: tuple[str, ...] = (
        "https://data.gov.tw/dataset/11452", "https://data.gov.tw/license",
        "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock",
    )
    limitations: tuple[str, ...] = (
        "latest_only_no_date_selector", "market_not_supplied", "dated_identity_evidence_required",
        "precise_publication_time_unknown", "dataset_metadata_monthly_vs_weekly_statistics",
        "unknown_csv_versions_rejected", "html_history_and_json_not_enabled",
        "reported_percent_rounding_not_recomputed",
    )


TDCC_MANIFEST = OwnershipSourceManifest()
