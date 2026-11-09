"""Provider-independent margin source contracts; no downloading or parsing IO.

Capabilities describe what was verified, independently of network enablement.
An HTTP request date is never evidence of the date of its response contents.
"""
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import (
    DateEvidence, FormulaPolicy, MarginError, QuantityUnit, SourceTiming,
    _aware, _date, _text, _uuid,
)


@dataclass(frozen=True, kw_only=True)
class MarginSourceRequest:
    """A request date selects a resource; it is never response-date evidence."""
    run_id: UUID
    source_id: UUID
    requested_date: date | None = None

    def __post_init__(self):
        _uuid(self.run_id, "run_id")
        _uuid(self.source_id, "source_id")
        if self.requested_date is not None:
            _date(self.requested_date, "requested_trade_date")


@dataclass(frozen=True, kw_only=True)
class MarginSourceSnapshot:
    """Unparsed source bytes for a future raw archive, not canonical data."""
    request: MarginSourceRequest
    body: bytes
    content_type: str
    fetched_at: datetime
    source_contract_version: str

    def __post_init__(self):
        if not isinstance(self.request, MarginSourceRequest):
            raise MarginError("invalid_source_request")
        if not isinstance(self.body, bytes) or len(self.body) > 20_000_000:
            raise MarginError("invalid_source_body")
        _text(self.content_type, "content_type")
        _aware(self.fetched_at, "fetched_at")
        _text(self.source_contract_version, "source_contract_version")


class SourceCapability(StrEnum):
    LATEST_ONLY = "latest_only"
    AUDITED_DATES_ONLY = "audited_dates_only"


class SourceDateFormat(StrEnum):
    UNDATED = "undated"
    ROC_YYYMMDD = "roc_yyyMMdd"
    GREGORIAN_YYYYMMDD = "gregorian_yyyyMMdd"


@dataclass(frozen=True, kw_only=True)
class SourceField:
    source_name: str
    domain_name: str
    unit: str

    def __post_init__(self):
        for value in (self.source_name, self.domain_name, self.unit):
            if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
                raise MarginError("invalid_source_field")


@dataclass(frozen=True, kw_only=True)
class MarginSourceContract:
    key: str
    version: str
    schema_version: str
    capability: SourceCapability
    quantity_unit: QuantityUnit
    date_field: str | None
    date_format: SourceDateFormat
    fields: tuple[SourceField, ...]
    financing_policy: FormulaPolicy
    short_policy: FormulaPolicy
    verified_historical_dates: tuple[date, ...] = ()
    column_contract_verified: bool = True

    def __post_init__(self):
        for value in (self.key, self.version, self.schema_version):
            if not isinstance(value, str) or not value or value != value.strip():
                raise MarginError("invalid_source_contract_version")
        if not isinstance(self.capability, SourceCapability):
            raise MarginError("invalid_source_capability")
        if not isinstance(self.quantity_unit, QuantityUnit):
            raise MarginError("invalid_source_quantity_unit")
        if not isinstance(self.date_format, SourceDateFormat):
            raise MarginError("invalid_source_date_format")
        if not isinstance(self.fields, tuple) or not self.fields or not all(
            isinstance(field, SourceField) for field in self.fields
        ):
            raise MarginError("invalid_source_fields")
        if (len({field.source_name for field in self.fields}) != len(self.fields)
                or len({field.domain_name for field in self.fields}) != len(self.fields)):
            raise MarginError("duplicate_source_contract_fields")
        if self.date_format is SourceDateFormat.UNDATED:
            if self.date_field is not None:
                raise MarginError("invalid_source_date_contract")
        elif (not isinstance(self.date_field, str)
                or self.date_field not in self.source_fields):
            raise MarginError("invalid_source_date_contract")
        if not all(isinstance(policy, FormulaPolicy) for policy in (
            self.financing_policy, self.short_policy,
        )):
            raise MarginError("invalid_source_formula_policy")
        if (not isinstance(self.verified_historical_dates, tuple)
                or any(type(day) is not date for day in self.verified_historical_dates)
                or tuple(sorted(set(self.verified_historical_dates))) != self.verified_historical_dates):
            raise MarginError("invalid_verified_historical_dates")
        if self.capability is SourceCapability.LATEST_ONLY and self.verified_historical_dates:
            raise MarginError("latest_source_cannot_claim_history")
        if type(self.column_contract_verified) is not bool:
            raise MarginError("invalid_column_contract_verification")

    @property
    def source_fields(self) -> tuple[str, ...]:
        return tuple(field.source_name for field in self.fields)

    def validate_request(self, requested_date: date | None = None) -> None:
        """Do not silently ignore a caller's historical-date request."""
        if requested_date is not None and type(requested_date) is not date:
            raise MarginError("invalid_requested_trade_date")
        if requested_date is None:
            return
        if self.capability is SourceCapability.LATEST_ONLY:
            raise MarginError("source_does_not_support_historical_date")
        if requested_date not in self.verified_historical_dates:
            raise MarginError("historical_date_contract_not_verified")

    def validate_columns(self, columns: tuple[str, ...]) -> None:
        """Exact names, independent of positions; no guessing unknown schemas."""
        if not self.column_contract_verified:
            raise MarginError("source_column_contract_not_verified")
        if not isinstance(columns, tuple) or any(not isinstance(value, str) for value in columns):
            raise MarginError("invalid_source_columns")
        if len(set(columns)) != len(columns):
            raise MarginError("duplicate_source_columns")
        if set(columns) != set(self.source_fields):
            raise MarginError("source_schema_mismatch")

    def date_blockers(self, timing: SourceTiming) -> tuple[str, ...]:
        if not isinstance(timing, SourceTiming):
            raise MarginError("invalid_source_timing")
        if timing.trade_date is None or timing.date_evidence is DateEvidence.UNVERIFIED:
            return ("trusted_source_trade_date_required",)
        reasons = []
        if timing.trade_date > timing.first_seen_at.astimezone(ZoneInfo(timing.timezone)).date():
            reasons.append("future_source_date")
        if self.date_field is None:
            # M1 has no Date. A fabricated field, fetch date, request date or
            # another dataset's date cannot turn this snapshot into daily data.
            if timing.date_evidence is not DateEvidence.VERIFIED_PAYLOAD:
                reasons.append("undated_source_requires_reviewed_payload_date_evidence")
        elif timing.date_evidence is not DateEvidence.SOURCE_FIELD:
            reasons.append("official_response_date_field_required")
        return tuple(reasons)

    def canonical_blockers(self, timing: SourceTiming) -> tuple[str, ...]:
        reasons = list(self.date_blockers(timing))
        if not self.column_contract_verified:
            reasons.append("source_column_contract_not_verified")
        if (self.capability is SourceCapability.AUDITED_DATES_ONLY
                and timing.trade_date not in self.verified_historical_dates):
            reasons.append("historical_date_contract_not_verified")
        return tuple(reasons)
