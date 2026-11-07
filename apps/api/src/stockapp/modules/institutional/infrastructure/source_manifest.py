"""Versioned, reviewable source capabilities; no implicit network enablement."""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from types import MappingProxyType

from stockapp.modules.institutional.domain.models import QuantityUnit, TradeScope
from .twse_parser import (
    LEGACY_CONTRACT, LEGACY_FIELDS, LEGACY_SCOPE, MODERN_CONTRACT, MODERN_FIELDS,
    MODERN_SCOPE, PARSER_VERSION, TWSE_ENDPOINT, VERIFIED_DATES,
)
from .tpex_parser import (
    TPEX_CONTRACT_VERSION, TPEX_ENDPOINT, TPEX_OPENAPI_FIELDS, TPEX_PARSER_VERSION, TPEX_SCOPE,
)

MANIFEST_VERSION = "institutional-sources-1.0.0"


@dataclass(frozen=True, kw_only=True)
class ParserContract:
    version: str
    parser_version: str
    fields: tuple[str, ...]
    scope: TradeScope
    # Empty for a latest-only contract, never an implicit all-history range.
    verified_historical_dates: tuple[date, ...] = ()

    @property
    def schema_sha256(self) -> str:
        fields = sorted(field.strip() for field in self.fields)
        return sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class SourceManifest:
    key: str
    audit_id: str
    provider: str
    endpoint: str
    request_parameters: tuple[tuple[str, str], ...]
    capability: str
    authorization: str
    license: str | None
    evidence_urls: tuple[str, ...]
    contracts: tuple[ParserContract, ...]
    known_limitations: tuple[str, ...]
    method: str = "GET"
    version: str = MANIFEST_VERSION
    quantity_unit: QuantityUnit = QuantityUnit.SHARES
    source_quantity_unit: str = "shares"
    unit_factor: int = 1
    frequency: str = "daily"
    timezone: str = "Asia/Taipei"
    published_at: str | None = None
    official_rate_limit: str | None = None
    default_network_enabled: bool = False
    body_audited_on: date = date(2026, 11, 7)
    metadata_reviewed_on: date = date(2026, 11, 8)


TWSE_MANIFEST = SourceManifest(
    key="twse_t86", audit_id="I1", provider="TWSE", endpoint=TWSE_ENDPOINT,
    request_parameters=(("date", "YYYYMMDD"), ("selectType", "ALLBUT0999"), ("response", "json")),
    capability="audited_dates_only", authorization="pending", license=None,
    evidence_urls=("https://www.twse.com.tw/zh/trading/foreign/t86.html",
        "https://www.twse.com.tw/zh/terms/use.html"),
    contracts=(
        ParserContract(version=LEGACY_CONTRACT, parser_version=PARSER_VERSION,
            fields=LEGACY_FIELDS, scope=LEGACY_SCOPE,
            verified_historical_dates=tuple(sorted(VERIFIED_DATES[LEGACY_CONTRACT]))),
        ParserContract(version=MODERN_CONTRACT, parser_version=PARSER_VERSION,
            fields=MODERN_FIELDS, scope=MODERN_SCOPE,
            verified_historical_dates=tuple(sorted(VERIFIED_DATES[MODERN_CONTRACT]))),
    ),
    known_limitations=("automatic_download_rights_unconfirmed", "historical_taxonomy_boundary_unconfirmed",
        "only_two_sample_dates_verified", "selector_ALL_unverified", "historical_instrument_registry_required"),
)

TPEX_MANIFEST = SourceManifest(
    key="tpex_openapi", audit_id="I2", provider="TPEx", endpoint=TPEX_ENDPOINT,
    request_parameters=(), capability="latest_only", authorization="ogdl_dataset_documented",
    license="OGDL-1.0", evidence_urls=("https://data.gov.tw/dataset/11856",
        "https://www.tpex.org.tw/openapi/swagger.json", "https://data.gov.tw/license"),
    contracts=(ParserContract(version=TPEX_CONTRACT_VERSION, parser_version=TPEX_PARSER_VERSION,
        fields=TPEX_OPENAPI_FIELDS, scope=TPEX_SCOPE),),
    known_limitations=("dealer_proprietary_missing", "dealer_hedging_buy_and_net_missing",
        "trade_scope_unverified", "no_historical_date_parameter", "not_complete_institutional_report",
        "legacy_CSV_dataset_mapping_not_all_AL_history_permission"),
)

MANIFESTS = MappingProxyType({item.key: item for item in (TWSE_MANIFEST, TPEX_MANIFEST)})
