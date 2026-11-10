"""Audited securities-lending capabilities, not enabled HTTP adapters.

Website field mappings are semantic descriptions until literal grouped schemas
and use rights are reviewed. Only the M6 flat JSON field contract is verified.
"""
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from stockapp.modules.lending.domain.models import (
    AdjustmentTreatment, ChannelDefinition, FormulaPolicy, LendingChannel,
    QuantityUnit,
)
from stockapp.modules.lending.domain.sources import (
    LendingSourceContract, SourceCapability, SourceDateFormat, SourceField,
    SourceProduct,
)

MANIFEST_VERSION = 'lending-sources-1.1.0'
BORROWING_POLICY = FormulaPolicy(version='lending-borrowed-base-1.0.0',
    verified=True, adjustment_treatment=AdjustmentTreatment.NONE)
LENDING_SHORT_POLICY = FormulaPolicy(version='lending-short-signed-1.0.0',
    verified=True, adjustment_treatment=AdjustmentTreatment.SIGNED)


def _channel(kind, code, family, *, verified=False):
    return ChannelDefinition(channel=kind, code=code, family=family,
        version=family+'-channel-v1', verified=verified, coverage=frozenset())


TWSE_COMBINED_CHANNEL = _channel(LendingChannel.COMBINED, 'SLBNLB',
    'twse-lending-outstanding', verified=True)
TWSE_REPORTED_SHORT_CHANNEL = _channel(LendingChannel.UNSPECIFIED, 'unmapped',
    'twse-lending-short-reported')
TPEX_REPORTED_SHORT_CHANNEL = _channel(LendingChannel.UNSPECIFIED, 'unmapped',
    'tpex-lending-short-reported')
TPEX_REPORTED_FLOW_CHANNEL = _channel(LendingChannel.UNSPECIFIED, 'unmapped',
    'tpex-lending-short-flow')
TWSE_REPORTED_QUOTA_CHANNEL = _channel(LendingChannel.UNSPECIFIED, 'unmapped',
    'twse-reported-available-short-quota')


def _f(group, name, target, unit='shares'):
    return SourceField(group=group, source_name=name, domain_name=target, unit=unit)


_GROUPED_SHORT_FIELDS = (
    _f('response','date','trade_date','gregorian_yyyyMMdd'),
    _f('identity','代號','symbol','text'), _f('identity','名稱','name','text'),
    # These fields intentionally describe the adjacent margin group without
    # importing its calculator or using it in a securities-lending identity.
    _f('融券','前日餘額','excluded_margin.previous_balance'),
    _f('融券','賣出','excluded_margin.short_sell'),
    _f('融券','買進','excluded_margin.short_cover'),
    _f('融券','現券償還','excluded_margin.stock_repayment'),
    _f('融券','今日餘額','excluded_margin.balance'),
    _f('融券','限額','excluded_margin.limit'),
    _f('借券賣出','前日餘額','lending_short.previous_balance'),
    _f('借券賣出','賣出','lending_short.short_sales'),
    _f('借券賣出','還券','lending_short.short_returns'),
    _f('借券賣出','調整','lending_short.signed_adjustment'),
    _f('借券賣出','今日餘額','lending_short.balance'),
    _f('借券賣出','次日限額','regulatory_quota.quantity'),
    _f('record','註記','source_note','text'),
)
TWSE_SHORT_CONTRACT = LendingSourceContract(
    key='twse_lending_short_dated', version='twse-lending-short-audit-20161107-v1',
    schema_version='twse-twt93u-grouped-semantic-20161107-v1',
    product=SourceProduct.LENDING_SHORT_SALES, capability=SourceCapability.AUDITED_DATES_ONLY,
    quantity_unit=QuantityUnit.SHARES, fields=_GROUPED_SHORT_FIELDS,
    date_field=('response','date'), date_format=SourceDateFormat.GREGORIAN_YYYYMMDD,
    channel=TWSE_REPORTED_SHORT_CHANNEL, formula_policy=LENDING_SHORT_POLICY,
    verified_historical_dates=(date(2016,11,7),), formula_valid_from=date(2012,3,19),
    column_contract_verified=False,
)
TWSE_BORROWED_CONTRACT = LendingSourceContract(
    key='twse_borrowed_positions_combined', version='twse-borrowed-combined-audit-20161107-v1',
    schema_version='twse-twt72u-grouped-semantic-20161107-v1',
    product=SourceProduct.BORROWED_POSITIONS, capability=SourceCapability.AUDITED_DATES_ONLY,
    quantity_unit=QuantityUnit.SHARES, fields=(
        _f('response','date','trade_date','gregorian_yyyyMMdd'),
        _f('identity','市場別','market_label','source_label_unverified'),
        _f('identity','代號','symbol','text'), _f('identity','名稱','name','text'),
        _f('借券','前日借券餘額(1)股','borrowing.previous_balance'),
        _f('借券','本日異動股借券(2)','borrowing.borrowed_quantity'),
        _f('借券','本日異動股還券(3)','borrowing.returned_quantity'),
        _f('借券','本日借券餘額股(4)','borrowing.balance'),
        _f('估值','本日收盤價(5)','valuation.close','currency_per_share_unverified'),
        _f('估值','借券餘額市值(6)','valuation.market_value','currency_unverified')),
    date_field=('response','date'), date_format=SourceDateFormat.GREGORIAN_YYYYMMDD,
    channel=TWSE_COMBINED_CHANNEL, formula_policy=BORROWING_POLICY,
    verified_historical_dates=(date(2016,11,7),), column_contract_verified=False,
)
TPEX_SHORT_CONTRACT = LendingSourceContract(
    key='tpex_lending_short_openapi', version='tpex-lending-short-roc-latest-1.0.0',
    schema_version='tpex-margin-sbl-flat-16-fields-v1',
    product=SourceProduct.LENDING_SHORT_SALES, capability=SourceCapability.LATEST_ONLY,
    quantity_unit=QuantityUnit.SHARES, fields=(
        _f('record','Date','trade_date','roc_yyyMMdd'),
        _f('record','SecuritiesCompanyCode','symbol','text'),
        _f('record','CompanyName','name','text'),
        _f('record','SaleBalancePreviousDay','excluded_margin.previous_balance'),
        _f('record','SaleSell','excluded_margin.short_sell'),
        _f('record','SaleBuy','excluded_margin.short_cover'),
        _f('record','SaleSpotSecurities','excluded_margin.stock_repayment'),
        _f('record','SaleBalanceOfTheMarketDay','excluded_margin.balance'),
        _f('record','SaleAvailableVolumes','excluded_margin.limit'),
        _f('record','SecuritiesBorrowingBalancePreviousDay','lending_short.previous_balance'),
        _f('record','SecuritiesBorrowingSale','lending_short.short_sales'),
        _f('record','SecuritiesBorrowingReturn','lending_short.short_returns'),
        _f('record','SecuritiesBorrowingAdjustment','lending_short.signed_adjustment'),
        _f('record','SecuritiesBorrowingBalanceOfTheMarketDay','lending_short.balance'),
        _f('record','AvailableVolumesForSBLShortSale','regulatory_quota.quantity'),
        _f('record','Note','source_note','text')),
    date_field=('record','Date'), date_format=SourceDateFormat.ROC_YYYMMDD,
    channel=TPEX_REPORTED_SHORT_CHANNEL, formula_policy=LENDING_SHORT_POLICY,
)
TPEX_DATED_SHORT_CONTRACT = LendingSourceContract(
    key='tpex_lending_short_dated', version='tpex-lending-short-audit-20161107-v1',
    schema_version='tpex-sbl-grouped-semantic-20161107-v1',
    product=SourceProduct.LENDING_SHORT_SALES, capability=SourceCapability.AUDITED_DATES_ONLY,
    quantity_unit=QuantityUnit.SHARES, fields=_GROUPED_SHORT_FIELDS,
    date_field=('response','date'), date_format=SourceDateFormat.GREGORIAN_YYYYMMDD,
    channel=TPEX_REPORTED_SHORT_CHANNEL, formula_policy=LENDING_SHORT_POLICY,
    verified_historical_dates=(date(2016,11,7),), column_contract_verified=False,
)
TPEX_FLOW_CONTRACT = LendingSourceContract(
    key='tpex_lending_short_flow_candidate', version='tpex-short-sell-flow-candidate-v1',
    schema_version='tpex-short-sell-flat-7-fields-audit-v1',
    product=SourceProduct.DAILY_SHORT_FLOW, capability=SourceCapability.LATEST_ONLY,
    quantity_unit=QuantityUnit.LOTS, fields=(
        _f('record','Date','trade_date','roc_yyyMMdd'),
        _f('record','SecuritiesCompanyCode','symbol','text'),
        _f('record','CompanyName','name','text'),
        _f('record','ShortSaleVolume','excluded_margin.short_sell','lots'),
        _f('record','ShortSaleAmount','excluded_margin.short_sale_amount','amount_scale_unverified'),
        _f('record','SBLVolume','lending_short.short_sales','lots'),
        _f('record','SBLAmount','lending_short.short_sale_amount','amount_scale_unverified')),
    date_field=('record','Date'), date_format=SourceDateFormat.ROC_YYYMMDD,
    channel=TPEX_REPORTED_FLOW_CHANNEL, formula_policy=None,
)
TWSE_QUOTA_CONTRACT = LendingSourceContract(
    key='twse_short_quota_candidate', version='twse-twt96u-quota-candidate-v1',
    schema_version='twse-twt96u-swagger-4-fields-body-unverified-v1',
    product=SourceProduct.QUOTA_ONLY, capability=SourceCapability.LATEST_ONLY,
    quantity_unit=QuantityUnit.SHARES, fields=(
        _f('record','TWSECode','twse.symbol','text'),
        _f('record','TWSEAvailableVolume','twse.regulatory_quota.quantity'),
        _f('record','GRETAICode','tpex.symbol','text'),
        _f('record','GRETAIAvailableVolume','tpex.regulatory_quota.quantity')),
    date_field=None, date_format=SourceDateFormat.UNDATED,
    channel=TWSE_REPORTED_QUOTA_CHANNEL, formula_policy=None,
    body_verified=False, column_contract_verified=False,
)


@dataclass(frozen=True, kw_only=True)
class LendingSourceManifest:
    audit_id: str
    provider: str
    endpoint: str
    request_parameters: tuple[tuple[str,str], ...]
    contract: LendingSourceContract
    markets: tuple[str, ...]
    authorization: str
    license: str | None
    attribution: str
    evidence_urls: tuple[str, ...]
    response_format: str
    limitations: tuple[str, ...]
    declared_history_start: date | None = None
    body_audited_on: date | None = date(2026,11,7)
    metadata_reviewed_on: date = date(2026,11,7)
    method: str = 'GET'
    version: str = MANIFEST_VERSION
    frequency: str = '1d'
    timezone: str = 'Asia/Taipei'
    default_network_enabled: bool = False
    official_rate_limit: str | None = None
    source_published_at: str | None = None

    def observation_blockers(self, **observation):
        reasons = list(self.contract.observation_blockers(**observation))
        if self.authorization != 'ogdl_dataset_documented':
            reasons.append('source_use_rights_unconfirmed')
        return tuple(reasons)

    def report_blockers(self, report):
        reasons = list(self.contract.report_blockers(report))
        if self.authorization != 'ogdl_dataset_documented':
            reasons.append('source_use_rights_unconfirmed')
        return tuple(reasons)


TWSE_SHORT_MANIFEST = LendingSourceManifest(
    audit_id='M3', provider='TWSE',
    endpoint='https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U',
    request_parameters=(('date','YYYYMMDD'),('response','json')),
    contract=TWSE_SHORT_CONTRACT, markets=('TWSE',), authorization='pending', license=None,
    attribution='臺灣證券交易所：融券借券賣出餘額；自動利用／轉載使用權待核實',
    evidence_urls=('https://www.twse.com.tw/zh/trading/margin/twt93u.html',
                   'https://www.twse.com.tw/zh/terms/use.html'),
    response_format='JSON stat/date/fields/groups/data; duplicate labels require groups',
    declared_history_start=date(2005,7,1),
    limitations=('website_use_rights_unconfirmed','literal_grouped_schema_not_verified',
        'only_20161107_body_audited','pre_20120319_formula_not_verified',
        '20120319_boundary_not_continuous_schema_proof','system_channel_not_verified',
        'source_publication_time_unknown','downloader_and_parser_disabled'),
)
TWSE_BORROWED_MANIFEST = LendingSourceManifest(
    audit_id='M4', provider='TWSE',
    endpoint='https://www.twse.com.tw/rwd/zh/lending/TWT72U',
    request_parameters=(('date','YYYYMMDD'),('selectType','SLBNLB'),('response','json')),
    contract=TWSE_BORROWED_CONTRACT, markets=('TWSE','TPEX'),
    authorization='pending', license=None,
    attribution='臺灣證券交易所：借券餘額；自動利用／轉載使用權待核實',
    evidence_urls=('https://www.twse.com.tw/zh/products/sbl/disclosures/twt72u.html',
                   'https://www.twse.com.tw/zh/terms/use.html'),
    response_format='JSON stat/date/tables; market dispatch required',
    declared_history_start=date(2004,11,22),
    limitations=('website_use_rights_unconfirmed','literal_grouped_schema_not_verified',
        'only_20161107_SLBNLB_body_audited','channel_label_is_not_partition_proof',
        'SLB_NLB_LON_TWTBBU_are_unverified_separate_selector_contracts',
        'literal_market_labels_require_review','currency_scale_not_canonicalized',
        'source_publication_time_unknown','downloader_and_parser_disabled'),
)
TPEX_SHORT_MANIFEST = LendingSourceManifest(
    audit_id='M6', provider='TPEx',
    endpoint='https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl', request_parameters=(),
    contract=TPEX_SHORT_CONTRACT, markets=('TPEX',),
    authorization='ogdl_dataset_documented', license='OGDL-1.0',
    attribution='證券櫃檯買賣中心：上櫃股票融券借券賣出餘額；依政府資料開放授權條款第1版利用',
    evidence_urls=('https://data.gov.tw/dataset/11632',
                   'https://www.tpex.org.tw/openapi/swagger.json','https://data.gov.tw/license'),
    response_format='JSON array; prior Swagger declared object; source statistics date retained from body',
    metadata_reviewed_on=date(2026,11,10),
    limitations=('latest_only_no_date_parameter','SecuritiesBorrowing_means_lending_short_not_borrowed_outstanding',
        'Sale_group_belongs_to_margin','system_channel_not_verified',
        'quota_next_business_day_effectivity_requires_evidence',
        'precise_openapi_publication_time_unknown','dataset_legacy_csv_mapping_preserved',
        'swagger_metadata_403_on_20261110_prior_body_contract_retained',
        'manual_download_requires_explicit_opt_in'),
)
TPEX_DATED_SHORT_MANIFEST = LendingSourceManifest(
    audit_id='M7', provider='TPEx',
    endpoint='https://www.tpex.org.tw/www/zh-tw/margin/sbl',
    request_parameters=(('date','YYYY/MM/DD'),('response','json')),
    contract=TPEX_DATED_SHORT_CONTRACT, markets=('TPEX',),
    authorization='pending', license=None,
    attribution='證券櫃檯買賣中心：融券借券賣出餘額；網站自動利用／轉載使用權待核實',
    evidence_urls=('https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/sbl.html',),
    response_format='JSON stat/date/tables; semantic field mapping only',
    declared_history_start=date(2006,1,1),
    limitations=('website_use_rights_unconfirmed','literal_grouped_schema_not_verified',
        'only_20161107_body_audited','2012_historical_schema_segment_not_verified',
        'system_channel_not_verified','source_publication_time_unknown','downloader_and_parser_disabled'),
)
TPEX_FLOW_MANIFEST = LendingSourceManifest(
    audit_id='M8', provider='TPEx',
    endpoint='https://www.tpex.org.tw/openapi/v1/tpex_short_sell', request_parameters=(),
    contract=TPEX_FLOW_CONTRACT, markets=('TPEX',), authorization='pending', license=None,
    attribution='證券櫃檯買賣中心：當日融券與借券賣出成交量值；使用權及金額單位待核實',
    evidence_urls=('https://www.tpex.org.tw/openapi/swagger.json',),
    response_format='JSON array observed; separate daily flows, no balances',
    limitations=('source_dataset_license_mapping_unconfirmed','amount_scale_currency_unconfirmed',
        'lots_require_instrument_period_multiplier','daily_flow_is_not_balance',
        'no_historical_date_parameter','downloader_and_parser_disabled'),
)
TWSE_QUOTA_MANIFEST = LendingSourceManifest(
    audit_id='M9', provider='TWSE', endpoint='https://openapi.twse.com.tw/v1/SBL/TWT96U',
    request_parameters=(), contract=TWSE_QUOTA_CONTRACT, markets=('TWSE','TPEX'),
    authorization='resource_mapping_unconfirmed', license=None,
    attribution='臺灣證券交易所：可借券賣出股數候選來源；僅額度非實際成交',
    evidence_urls=('https://openapi.twse.com.tw/v1/swagger.json','https://data.gov.tw/dataset/11872'),
    response_format='Swagger object/JSON/CSV declared; response body not verified',
    body_audited_on=None,
    limitations=('quota_only_not_borrowed_or_sold','body_not_verified','undated',
        'dataset11872_resource_mapping_unconfirmed','publication_and_refresh_unknown',
        'quota_effectivity_not_verified','downloader_and_parser_disabled'),
)

MANIFESTS = MappingProxyType({item.audit_id:item for item in (
    TWSE_SHORT_MANIFEST,TWSE_BORROWED_MANIFEST,TPEX_SHORT_MANIFEST,
    TPEX_DATED_SHORT_MANIFEST,TPEX_FLOW_MANIFEST,TWSE_QUOTA_MANIFEST)})
