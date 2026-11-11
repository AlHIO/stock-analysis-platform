"""Versioned official margin capabilities; approved manual downloads opt in.

The Phase 2A body audit and a subsequent Swagger/metadata review are distinct.
Swagger describes objects; the audited live bodies were arrays/tables. Existing
adapters enforce each reviewed contract; disabled candidate routes remain metadata
until their literal schema and use rights are verified.
"""
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from uuid import UUID

from stockapp.modules.margin.domain.models import (
    AdjustmentTreatment, ConversionEvidence, FormulaPolicy, MarginError,
    QuantityUnit, SourceTiming,
)
from stockapp.modules.margin.domain.sources import (
    MarginSourceContract, SourceCapability, SourceDateFormat, SourceField,
)

MANIFEST_VERSION = "margin-sources-1.1.0"


def _field(name: str, target: str, unit: str = "lots") -> SourceField:
    return SourceField(source_name=name, domain_name=target, unit=unit)


_FINANCING_POLICY = FormulaPolicy(
    version="margin-financing-base-1.0.0", verified=True,
    adjustment_treatment=AdjustmentTreatment.NONE,
)
_SHORT_POLICY = FormulaPolicy(
    version="margin-short-base-1.0.0", verified=True,
    adjustment_treatment=AdjustmentTreatment.NONE,
)
_TWSE_FIELDS = (
    _field("股票代號", "symbol", "text"), _field("股票名稱", "name", "text"),
    _field("融資買進", "financing.purchase"), _field("融資賣出", "financing.sale"),
    _field("融資現金償還", "financing.cash_repayment"),
    _field("融資前日餘額", "financing.previous_balance"),
    _field("融資今日餘額", "financing.balance"), _field("融資限額", "financing.limit"),
    _field("融券買進", "short_margin.short_cover"), _field("融券賣出", "short_margin.short_sell"),
    _field("融券現券償還", "short_margin.stock_repayment"),
    _field("融券前日餘額", "short_margin.previous_balance"),
    _field("融券今日餘額", "short_margin.balance"), _field("融券限額", "short_margin.limit"),
    _field("資券互抵", "offsetting"), _field("註記", "note", "text"),
)
_TPEX_FIELDS = (
    _field("Date", "trade_date", "roc_yyyMMdd"),
    _field("SecuritiesCompanyCode", "symbol", "text"), _field("CompanyName", "name", "text"),
    _field("MarginPurchaseBalancePreviousDay", "financing.previous_balance"),
    _field("MarginPurchase", "financing.purchase"), _field("MarginSales", "financing.sale"),
    _field("CashRedemption", "financing.cash_repayment"),
    _field("MarginPurchaseBalance", "financing.balance"),
    _field("MarginPurchaseBalanceBelongSecuritiesFinanceEnterprise", "financing.finance_enterprise_balance"),
    _field("MarginPurchaseUtilizationRate", "financing.reported_utilization_rate", "percent"),
    _field("MarginPurchaseQuota", "financing.limit"),
    _field("ShortSaleBalancePreviousDay", "short_margin.previous_balance"),
    _field("ShortSale", "short_margin.short_sell"),
    _field("ShortConvering", "short_margin.short_cover"),
    _field("StockRedemption", "short_margin.stock_repayment"),
    _field("ShortSaleBalance", "short_margin.balance"),
    _field("ShortSaleBalanceBelongSecuritiesFinanceEnterprise", "short_margin.finance_enterprise_balance"),
    _field("ShortSaleUtilizationRate", "short_margin.reported_utilization_rate", "percent"),
    _field("ShortSaleQuota", "short_margin.limit"), _field("Offsetting", "offsetting"),
    _field("Note", "note", "text"),
)

TWSE_OPENAPI_CONTRACT = MarginSourceContract(
    key="twse_margin_openapi", version="twse-margin-undated-1.0.0",
    schema_version="twse-margin-openapi-16-fields-v1", capability=SourceCapability.LATEST_ONLY,
    quantity_unit=QuantityUnit.LOTS, date_field=None, date_format=SourceDateFormat.UNDATED,
    fields=_TWSE_FIELDS, financing_policy=_FINANCING_POLICY, short_policy=_SHORT_POLICY,
)
TWSE_DATED_CONTRACT = MarginSourceContract(
    key="twse_margin_dated", version="twse-margin-dated-audit-1.0.0",
    schema_version="twse-margin-tables-audited-20161107-v1",
    capability=SourceCapability.AUDITED_DATES_ONLY, quantity_unit=QuantityUnit.LOTS,
    date_field="date", date_format=SourceDateFormat.GREGORIAN_YYYYMMDD,
    # Dated table group labels must be retained; this is a semantic mapping,
    # not permission to parse flat duplicate headers or market summaries.
    fields=(_field("date", "trade_date", "gregorian_yyyyMMdd"), *_TWSE_FIELDS),
    financing_policy=_FINANCING_POLICY, short_policy=_SHORT_POLICY,
    verified_historical_dates=(date(2016, 11, 7),),
    column_contract_verified=False,
)
TPEX_CONTRACT = MarginSourceContract(
    key="tpex_margin_openapi", version="tpex-margin-roc-latest-1.0.0",
    schema_version="tpex-margin-openapi-21-fields-v1", capability=SourceCapability.LATEST_ONLY,
    quantity_unit=QuantityUnit.LOTS, date_field="Date", date_format=SourceDateFormat.ROC_YYYMMDD,
    fields=_TPEX_FIELDS, financing_policy=_FINANCING_POLICY, short_policy=_SHORT_POLICY,
)


@dataclass(frozen=True, kw_only=True)
class MarginSourceManifest:
    audit_id: str
    provider: str
    endpoint: str
    request_parameters: tuple[tuple[str, str], ...]
    contract: MarginSourceContract
    authorization: str
    license: str | None
    attribution: str
    evidence_urls: tuple[str, ...]
    response_format: str
    limitations: tuple[str, ...]
    method: str = "GET"
    version: str = MANIFEST_VERSION
    frequency: str = "1d"
    timezone: str = "Asia/Taipei"
    source_published_at: str | None = None
    official_rate_limit: str | None = None
    default_network_enabled: bool = False
    body_audited_on: date = date(2026, 11, 7)
    metadata_reviewed_on: date = date(2026, 11, 8)
    declared_history_start: date | None = None

    def canonical_blockers(
        self, *, timing: SourceTiming, instrument_id: UUID, market_id: UUID,
        conversion: ConversionEvidence | None = None,
    ) -> tuple[str, ...]:
        """Eligibility only; adapters, identity resolution and persistence follow later."""
        if not isinstance(instrument_id, UUID) or not isinstance(market_id, UUID):
            raise MarginError("invalid_instrument_binding")
        reasons = list(self.contract.canonical_blockers(timing))
        if self.authorization not in ("ogdl_dataset_documented", "ogdl_platform_documented"):
            reasons.append("source_use_rights_unconfirmed")
        if self.contract.quantity_unit is not QuantityUnit.SHARES:
            if conversion is None:
                reasons.append("verified_instrument_unit_multiplier_required")
            elif not isinstance(conversion, ConversionEvidence):
                raise MarginError("invalid_conversion_evidence")
            elif (conversion.instrument_id != instrument_id or conversion.market_id != market_id
                    or conversion.source_unit is not self.contract.quantity_unit
                    or conversion.target_unit not in (QuantityUnit.SHARES, QuantityUnit.UNITS)
                    or timing.trade_date is None
                    or not conversion.valid_from <= timing.trade_date <= conversion.valid_through):
                reasons.append("incompatible_unit_conversion_evidence")
        return tuple(reasons)


TWSE_OPENAPI_MANIFEST = MarginSourceManifest(
    audit_id="M1", provider="TWSE",
    metadata_reviewed_on=date(2026, 11, 9),
    endpoint="https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
    request_parameters=(), contract=TWSE_OPENAPI_CONTRACT,
    authorization="ogdl_platform_documented", license="OGDL-1.0",
    attribution="臺灣證券交易所：集中市場融資融券餘額；依政府資料開放授權條款第1版利用",
    evidence_urls=("https://openapi.twse.com.tw/v1/swagger.json", "https://data.gov.tw/license",
                   "https://www.twse.com.tw/zh/terms/use.html"),
    response_format="JSON array observed; Swagger declares object; CSV declared, not body-audited",
    limitations=("no_trade_date_field", "no_historical_date_parameter", "no_global_lot_multiplier",
                 "precise_publication_time_unknown", "no_verified_adjustment_field",
                 "manual_download_requires_explicit_opt_in"),
)
TWSE_DATED_MANIFEST = MarginSourceManifest(
    audit_id="M2", provider="TWSE",
    endpoint="https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
    request_parameters=(("date", "YYYYMMDD"), ("selectType", "ALL"), ("response", "json")),
    contract=TWSE_DATED_CONTRACT, authorization="pending", license=None,
    attribution="臺灣證券交易所：融資融券餘額；自動利用／轉載使用權待核實",
    evidence_urls=("https://www.twse.com.tw/zh/trading/margin/mi-margn.html",
                   "https://www.twse.com.tw/zh/terms/use.html"),
    response_format="JSON stat/date/tables; instrument rows separate from market summaries",
    declared_history_start=date(2001, 1, 1),
    limitations=("automatic_download_rights_unconfirmed", "only_20161107_body_audited",
                 "grouped_headers_and_market_summary_require_separate_parser",
                 "no_verified_contiguous_history", "no_global_lot_multiplier",
                 "no_verified_adjustment_field", "downloader_and_parser_disabled"),
)
TPEX_MANIFEST = MarginSourceManifest(
    audit_id="M5", provider="TPEx",
    endpoint="https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
    request_parameters=(), contract=TPEX_CONTRACT,
    authorization="ogdl_dataset_documented", license="OGDL-1.0",
    attribution="證券櫃檯買賣中心：上櫃股票融資融券餘額；依政府資料開放授權條款第1版利用",
    evidence_urls=("https://data.gov.tw/dataset/11387", "https://www.tpex.org.tw/openapi/swagger.json",
                   "https://data.gov.tw/license"),
    response_format="JSON array observed; Swagger declares object; CSV declared, not body-audited",
    limitations=("latest_only_no_historical_date_parameter", "no_global_lot_multiplier",
                 "short_convering_is_official_buy_to_cover_spelling", "no_verified_adjustment_field",
                 "note_may_describe_next_business_day", "quota_effective_date_requires_evidence",
                 "precise_publication_time_unknown", "legacy_csv_metadata_mapping_preserved",
                 "manual_download_requires_explicit_opt_in"),
)

MANIFESTS = MappingProxyType({
    item.contract.key: item for item in (TWSE_OPENAPI_MANIFEST, TWSE_DATED_MANIFEST, TPEX_MANIFEST)
})

# These are different products and cannot substitute missing financing fields.
DEFERRED_SOURCE_IDS = MappingProxyType({
    "M3": "short_margin_and_lending_short_dated_website_rights_pending",
    "M4": "securities_lending_balance_separate_ledger_rights_pending",
    "M6": "short_margin_and_lending_short_not_financing_balance_source",
    "M7": "lending_short_dated_website_not_margin_financing_history",
    "M8": "daily_short_flows_not_balance_amount_scale_and_rights_pending",
    "M9": "lending_short_available_quota_undated_body_not_verified",
})
