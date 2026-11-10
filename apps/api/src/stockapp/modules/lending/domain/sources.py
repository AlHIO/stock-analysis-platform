"""Provider-neutral lending capabilities; neither requests nor quota imply facts."""
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import (
    AdjustmentTreatment, ChannelDefinition, DateEvidence, FormulaPolicy, LendingDailyReport,
    LendingError, QuantityUnit,
    ReportKind, SourceTiming,
)


def _text(value, name):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 256 or '\x00' in value):
        raise LendingError('invalid_' + name)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise LendingError('invalid_' + name) from None


class SourceProduct(StrEnum):
    BORROWED_POSITIONS = 'borrowed_positions'
    LENDING_SHORT_SALES = 'lending_short_sales'
    DAILY_SHORT_FLOW = 'daily_short_flow'
    QUOTA_ONLY = 'quota_only'


class SourceCapability(StrEnum):
    LATEST_ONLY = 'latest_only'
    AUDITED_DATES_ONLY = 'audited_dates_only'


class SourceDateFormat(StrEnum):
    UNDATED = 'undated'
    ROC_YYYMMDD = 'roc_yyyMMdd'
    GREGORIAN_YYYYMMDD = 'gregorian_yyyyMMdd'


@dataclass(frozen=True, kw_only=True)
class LendingSourceRequest:
    run_id: UUID
    source_id: UUID
    source_key: str
    requested_date: date | None = None
    channel_code: str | None = None

    def __post_init__(self):
        if not isinstance(self.run_id, UUID) or not isinstance(self.source_id, UUID):
            raise LendingError('invalid_source_request_identity')
        _text(self.source_key, 'source_key')
        if self.requested_date is not None and type(self.requested_date) is not date:
            raise LendingError('invalid_requested_trade_date')
        if self.channel_code is not None:
            _text(self.channel_code, 'requested_channel')


@dataclass(frozen=True, kw_only=True)
class LendingSourceSnapshot:
    request: LendingSourceRequest
    body: bytes
    content_type: str
    fetched_at: datetime
    source_contract_version: str

    def __post_init__(self):
        if not isinstance(self.request, LendingSourceRequest):
            raise LendingError('invalid_source_request')
        if not isinstance(self.body, bytes) or len(self.body) > 20_000_000:
            raise LendingError('invalid_source_body')
        _text(self.content_type, 'content_type')
        _text(self.source_contract_version, 'source_contract_version')
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.utcoffset() is None:
            raise LendingError('invalid_fetched_at')


@dataclass(frozen=True, kw_only=True)
class SourceField:
    """A group-qualified field; duplicate text across groups is not a collision."""
    group: str
    source_name: str
    domain_name: str
    unit: str

    def __post_init__(self):
        for name in ('group', 'source_name', 'domain_name', 'unit'):
            _text(getattr(self, name), 'source_field_' + name)

    @property
    def key(self):
        return self.group, self.source_name


@dataclass(frozen=True, kw_only=True)
class LendingSourceContract:
    key: str
    version: str
    schema_version: str
    product: SourceProduct
    capability: SourceCapability
    quantity_unit: QuantityUnit
    fields: tuple[SourceField, ...]
    date_field: tuple[str, str] | None
    date_format: SourceDateFormat
    channel: ChannelDefinition
    formula_policy: FormulaPolicy | None
    verified_historical_dates: tuple[date, ...] = ()
    formula_valid_from: date | None = None
    column_contract_verified: bool = True
    body_verified: bool = True

    def __post_init__(self):
        for name in ('key', 'version', 'schema_version'):
            _text(getattr(self, name), 'source_' + name)
        if not isinstance(self.product, SourceProduct) or not isinstance(self.capability, SourceCapability):
            raise LendingError('invalid_source_capability')
        if not isinstance(self.quantity_unit, QuantityUnit) or not isinstance(self.date_format, SourceDateFormat):
            raise LendingError('invalid_source_unit_or_date_format')
        if not isinstance(self.channel, ChannelDefinition):
            raise LendingError('invalid_source_channel')
        if (not isinstance(self.fields, tuple) or not self.fields
                or not all(isinstance(field, SourceField) for field in self.fields)):
            raise LendingError('invalid_source_fields')
        if (len({field.key for field in self.fields}) != len(self.fields)
                or len({field.domain_name for field in self.fields}) != len(self.fields)):
            raise LendingError('duplicate_source_contract_fields')
        if self.date_format is SourceDateFormat.UNDATED:
            if self.date_field is not None:
                raise LendingError('invalid_source_date_contract')
        elif self.date_field not in self.source_fields:
            raise LendingError('invalid_source_date_contract')
        balance = self.product in {SourceProduct.BORROWED_POSITIONS, SourceProduct.LENDING_SHORT_SALES}
        if ((balance and not isinstance(self.formula_policy, FormulaPolicy))
                or (not balance and self.formula_policy is not None)):
            raise LendingError('invalid_source_formula_policy')
        if self.formula_policy is not None and not self.formula_policy.verified:
            raise LendingError('unverified_source_formula_policy')
        treatment = {
            SourceProduct.BORROWED_POSITIONS: AdjustmentTreatment.NONE,
            SourceProduct.LENDING_SHORT_SALES: AdjustmentTreatment.SIGNED,
        }.get(self.product)
        if self.formula_policy is not None and self.formula_policy.adjustment_treatment is not treatment:
            raise LendingError('source_product_formula_mismatch')
        if (not isinstance(self.verified_historical_dates, tuple)
                or any(type(day) is not date for day in self.verified_historical_dates)
                or tuple(sorted(set(self.verified_historical_dates))) != self.verified_historical_dates):
            raise LendingError('invalid_verified_historical_dates')
        if self.capability is SourceCapability.LATEST_ONLY and self.verified_historical_dates:
            raise LendingError('latest_source_cannot_claim_history')
        if self.formula_valid_from is not None and type(self.formula_valid_from) is not date:
            raise LendingError('invalid_formula_effective_date')
        if type(self.column_contract_verified) is not bool or type(self.body_verified) is not bool:
            raise LendingError('invalid_source_verification')

    @property
    def source_fields(self):
        return tuple(field.key for field in self.fields)

    def validate_request(self, requested_date: date | None = None, *, channel_code: str | None = None):
        if requested_date is not None and type(requested_date) is not date:
            raise LendingError('invalid_requested_trade_date')
        if channel_code is not None and channel_code != self.channel.code:
            raise LendingError('source_channel_not_verified')
        if requested_date is None:
            if self.capability is SourceCapability.AUDITED_DATES_ONLY:
                raise LendingError('dated_source_requires_requested_date')
            return
        if self.capability is SourceCapability.LATEST_ONLY:
            raise LendingError('source_does_not_support_historical_date')
        if requested_date not in self.verified_historical_dates:
            raise LendingError('historical_date_contract_not_verified')

    def validate_columns(self, columns: tuple[tuple[str, str], ...]):
        """Metadata-only grouped labels never become an enabled table parser."""
        if not self.column_contract_verified or not self.body_verified:
            raise LendingError('source_column_contract_not_verified')
        if (not isinstance(columns, tuple)
                or any(not isinstance(value, tuple) or len(value) != 2
                       or any(not isinstance(part, str) for part in value) for value in columns)):
            raise LendingError('invalid_source_columns')
        if len(set(columns)) != len(columns):
            raise LendingError('duplicate_source_columns')
        if set(columns) != set(self.source_fields):
            raise LendingError('source_schema_mismatch')

    def date_blockers(self, timing: SourceTiming):
        if not isinstance(timing, SourceTiming):
            raise LendingError('invalid_source_timing')
        if timing.trade_date is None or timing.date_evidence is DateEvidence.UNVERIFIED:
            return ('trusted_source_trade_date_required',)
        reasons = []
        if timing.trade_date > timing.first_seen_at.astimezone(ZoneInfo(timing.timezone)).date():
            reasons.append('future_source_date')
        if self.date_field is None:
            if timing.date_evidence is not DateEvidence.VERIFIED_PAYLOAD:
                reasons.append('undated_source_requires_reviewed_payload_date_evidence')
        elif timing.date_evidence is not DateEvidence.SOURCE_FIELD:
            reasons.append('official_response_date_field_required')
        if (self.capability is SourceCapability.AUDITED_DATES_ONLY
                and timing.trade_date not in self.verified_historical_dates):
            reasons.append('historical_date_contract_not_verified')
        if self.formula_valid_from is not None and timing.trade_date < self.formula_valid_from:
            reasons.append('historical_formula_not_verified')
        return tuple(reasons)

    def observation_blockers(
        self, *, product: SourceProduct, report_kind: ReportKind | None,
        source_contract_version: str, schema_version: str, quantity_unit: QuantityUnit,
        channel: ChannelDefinition, formula_policy: FormulaPolicy | None, timing: SourceTiming,
    ):
        if product is not self.product:
            raise LendingError('source_product_mismatch')
        expected = {
            SourceProduct.BORROWED_POSITIONS: ReportKind.BORROWED_POSITIONS,
            SourceProduct.LENDING_SHORT_SALES: ReportKind.LENDING_SHORT_SALES,
        }.get(self.product)
        if report_kind is not expected:
            raise LendingError('source_report_kind_mismatch')
        if source_contract_version != self.version or schema_version != self.schema_version:
            raise LendingError('source_schema_or_contract_mismatch')
        if quantity_unit is not self.quantity_unit:
            raise LendingError('source_quantity_unit_mismatch')
        if channel != self.channel:
            raise LendingError('source_channel_mismatch')
        if formula_policy != self.formula_policy:
            raise LendingError('source_formula_policy_mismatch')
        reasons = list(self.date_blockers(timing))
        if not self.body_verified:
            reasons.append('source_body_not_verified')
        if not self.column_contract_verified:
            reasons.append('source_column_contract_not_verified')
        return tuple(reasons)

    def report_blockers(self, report: LendingDailyReport):
        if not isinstance(report, LendingDailyReport):
            raise LendingError('invalid_lending_report')
        return self.observation_blockers(
            product=SourceProduct(report.report_kind.value), report_kind=report.report_kind,
            source_contract_version=report.lineage.source_contract_version,
            schema_version=report.lineage.schema_version, quantity_unit=report.quantity_unit,
            channel=report.channel, formula_policy=report.ledger.formula_policy,
            timing=report.timing,
        )
