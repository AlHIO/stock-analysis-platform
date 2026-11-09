"""Immutable margin facts; source dates, units and missing states stay explicit.

Balances are source assertions, not flows. In particular, a later report's
previous balance need not equal the first publication of an earlier report.
No constructor reconciles two dates or promotes a revision.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


class MarginError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value, name: str):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 256 or "\x00" in value):
        raise MarginError("invalid_" + name)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise MarginError("invalid_" + name) from None


def _date(value, name: str):
    if type(value) is not date:
        raise MarginError("invalid_" + name)


def _aware(value, name: str):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise MarginError("invalid_" + name)


def _uuid(value, name: str):
    if not isinstance(value, UUID):
        raise MarginError("invalid_" + name)


def _enum(value, cls, name: str):
    if not isinstance(value, cls):
        raise MarginError("invalid_" + name)


class QuantityUnit(StrEnum):
    SHARES = "shares"
    LOTS = "lots"
    UNITS = "units"


class Availability(StrEnum):
    REPORTED = "reported"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, kw_only=True)
class Quantity:
    """An exact source quantity, retaining null's financial meaning.

The value object permits signed numbers for adjustments. Each business role
checks whether a negative value is admissible; a negative balance is not one.
"""
    value: int | None
    unit: QuantityUnit
    availability: Availability

    def __post_init__(self):
        _enum(self.unit, QuantityUnit, "quantity_unit")
        _enum(self.availability, Availability, "availability")
        if self.availability is Availability.REPORTED:
            if type(self.value) is not int or not BIGINT_MIN <= self.value <= BIGINT_MAX:
                raise MarginError("invalid_quantity")
        elif self.value is not None:
            raise MarginError("unavailable_quantity_has_value")


class AdjustmentTreatment(StrEnum):
    NONE = "none"
    SIGNED = "signed"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, kw_only=True)
class FormulaPolicy:
    """An assertion by a reviewed source contract, never a guessed formula.

NONE means the verified formula has no adjustment term. Its source field must
be explicitly not applicable, rather than an unknown adjustment filled with 0.
SIGNED requires an observed signed term for a complete identity calculation.
"""
    version: str
    verified: bool
    adjustment_treatment: AdjustmentTreatment

    def __post_init__(self):
        _text(self.version, "formula_policy_version")
        if type(self.verified) is not bool:
            raise MarginError("invalid_formula_verification")
        _enum(self.adjustment_treatment, AdjustmentTreatment, "adjustment_treatment")


def _ledger(values: tuple[Quantity, ...], adjustment: Quantity, policy: FormulaPolicy):
    if not all(isinstance(value, Quantity) for value in (*values, adjustment)):
        raise MarginError("invalid_ledger_quantity")
    if len({value.unit for value in (*values, adjustment)}) != 1:
        raise MarginError("incompatible_quantity_units")
    if any(value.value is not None and value.value < 0 for value in values):
        raise MarginError("negative_balance_or_flow")
    if not isinstance(policy, FormulaPolicy):
        raise MarginError("invalid_formula_policy")
    if policy.adjustment_treatment is AdjustmentTreatment.NONE and adjustment.availability is not Availability.NOT_APPLICABLE:
        raise MarginError("unexpected_adjustment")


@dataclass(frozen=True, kw_only=True)
class FinancingLedger:
    previous_balance: Quantity
    purchase: Quantity
    sale: Quantity
    cash_repayment: Quantity
    balance: Quantity
    signed_adjustment: Quantity
    formula_policy: FormulaPolicy

    def __post_init__(self):
        _ledger((self.previous_balance, self.purchase, self.sale,
                 self.cash_repayment, self.balance), self.signed_adjustment, self.formula_policy)

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.balance.unit


@dataclass(frozen=True, kw_only=True)
class ShortMarginLedger:
    previous_balance: Quantity
    short_sell: Quantity
    short_cover: Quantity
    stock_repayment: Quantity
    balance: Quantity
    signed_adjustment: Quantity
    formula_policy: FormulaPolicy

    def __post_init__(self):
        _ledger((self.previous_balance, self.short_sell, self.short_cover,
                 self.stock_repayment, self.balance), self.signed_adjustment, self.formula_policy)

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.balance.unit


class LedgerKind(StrEnum):
    FINANCING = "financing"
    SHORT_MARGIN = "short_margin"


class DateEvidence(StrEnum):
    SOURCE_FIELD = "source_field"
    VERIFIED_PAYLOAD = "verified_payload"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, kw_only=True)
class SourceTiming:
    """Statistics date is independent of observation and publication timestamps.

VERIFIED_PAYLOAD is reviewed evidence binding this exact payload to a date.
HTTP headers, the request date and an unrelated dataset's date do not qualify.
Undated snapshots can retain timing but cannot become MarginDailyReport.
"""
    trade_date: date | None
    date_evidence: DateEvidence
    date_evidence_version: str | None
    first_seen_at: datetime
    source_published_at: datetime | None = None
    source_published_on: date | None = None
    date_evidence_payload_sha256: str | None = None
    timezone: str = "Asia/Taipei"

    def __post_init__(self):
        _enum(self.date_evidence, DateEvidence, "date_evidence")
        _aware(self.first_seen_at, "first_seen_at")
        _text(self.timezone, "timezone")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise MarginError("invalid_timezone") from None
        if self.trade_date is not None:
            _date(self.trade_date, "trade_date")
        if self.date_evidence_version is not None:
            _text(self.date_evidence_version, "date_evidence_version")
        if self.date_evidence is not DateEvidence.UNVERIFIED and (
                self.trade_date is None or self.date_evidence_version is None):
            raise MarginError("missing_trusted_source_date")
        if self.date_evidence_payload_sha256 is not None and (
                not isinstance(self.date_evidence_payload_sha256, str)
                or re.fullmatch("[0-9a-f]{64}", self.date_evidence_payload_sha256) is None):
            raise MarginError("invalid_date_evidence_payload_hash")
        if (self.date_evidence is DateEvidence.VERIFIED_PAYLOAD
                and self.date_evidence_payload_sha256 is None):
            raise MarginError("missing_date_evidence_payload_hash")
        if self.source_published_at is not None:
            _aware(self.source_published_at, "source_published_at")
            if self.source_published_at > self.first_seen_at:
                raise MarginError("publication_after_first_seen")
        if self.source_published_on is not None:
            _date(self.source_published_on, "source_published_on")
            zone = ZoneInfo(self.timezone)
            if self.source_published_on > self.first_seen_at.astimezone(zone).date():
                raise MarginError("publication_after_first_seen")
            if (self.source_published_at is not None
                    and self.source_published_on != self.source_published_at.astimezone(zone).date()):
                raise MarginError("inconsistent_publication_date")


class EffectiveRelation(StrEnum):
    SAME_DAY = "same_day"
    NEXT_BUSINESS_DAY = "next_business_day"
    UNSPECIFIED = "unspecified"


class QuotaBasis(StrEnum):
    BALANCE_CEILING = "balance_ceiling"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, kw_only=True)
class QuotaLimit:
    """Reported limit, not a derived available quantity.

A next-business-day relation may retain effective_on=None until a verified
calendar is supplied. No calendar date or balance-ceiling basis is inferred.
"""
    ledger: LedgerKind
    quantity: Quantity
    report_date: date
    effective_on: date | None
    effective_relation: EffectiveRelation
    basis: QuotaBasis
    evidence_version: str | None

    def __post_init__(self):
        _enum(self.ledger, LedgerKind, "ledger_kind")
        _enum(self.effective_relation, EffectiveRelation, "effective_relation")
        _enum(self.basis, QuotaBasis, "quota_basis")
        if not isinstance(self.quantity, Quantity):
            raise MarginError("invalid_quota_quantity")
        if self.quantity.value is not None and self.quantity.value < 0:
            raise MarginError("negative_quota")
        _date(self.report_date, "quota_report_date")
        if self.effective_on is not None:
            _date(self.effective_on, "quota_effective_on")
            if self.effective_on < self.report_date:
                raise MarginError("invalid_quota_effective_on")
            if self.effective_relation is EffectiveRelation.SAME_DAY and self.effective_on != self.report_date:
                raise MarginError("incompatible_quota_effectivity")
            if self.effective_relation is EffectiveRelation.NEXT_BUSINESS_DAY and self.effective_on <= self.report_date:
                raise MarginError("incompatible_quota_effectivity")
        if self.evidence_version is not None:
            _text(self.evidence_version, "quota_evidence_version")


@dataclass(frozen=True, kw_only=True)
class ConversionEvidence:
    """Instrument- and period-specific source-unit multiplier, never a default."""
    instrument_id: UUID
    market_id: UUID
    source_unit: QuantityUnit
    target_unit: QuantityUnit
    factor: Decimal
    valid_from: date
    valid_through: date
    evidence_version: str

    def __post_init__(self):
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        _enum(self.source_unit, QuantityUnit, "source_unit")
        _enum(self.target_unit, QuantityUnit, "target_unit")
        _date(self.valid_from, "conversion_valid_from")
        _date(self.valid_through, "conversion_valid_through")
        _text(self.evidence_version, "conversion_evidence_version")
        if self.valid_from > self.valid_through:
            raise MarginError("invalid_conversion_effective_range")
        if self.source_unit is not QuantityUnit.LOTS or self.target_unit not in (QuantityUnit.SHARES, QuantityUnit.UNITS):
            raise MarginError("unsupported_conversion_units")
        if not isinstance(self.factor, Decimal) or not self.factor.is_finite() or self.factor <= 0:
            raise MarginError("invalid_conversion_factor")
        # Exact NUMERIC(32,8)-compatible source evidence; no ambient rounding.
        _, digits, exponent = self.factor.as_tuple()
        if max(-exponent, 0) > 8 or max(len(digits) + exponent, 0) > 24:
            raise MarginError("conversion_factor_precision_exceeded")


@dataclass(frozen=True, kw_only=True)
class MarginSourceLineage:
    source_id: UUID
    run_id: UUID
    raw_id: UUID
    revision_id: UUID
    source_contract_version: str
    schema_version: str
    parser_version: str
    identity_evidence_version: str
    scope: str
    formula_version: str
    promotion_policy_version: str
    payload_sha256: str
    source_row_locator: str
    conversion_evidence: ConversionEvidence | None = None

    def __post_init__(self):
        for name in ("source_id", "run_id", "raw_id", "revision_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_contract_version", "schema_version", "parser_version",
                     "identity_evidence_version", "scope", "formula_version",
                     "promotion_policy_version", "source_row_locator"):
            _text(getattr(self, name), name)
        if not isinstance(self.payload_sha256, str) or re.fullmatch("[0-9a-f]{64}", self.payload_sha256) is None:
            raise MarginError("invalid_payload_sha256")
        if self.conversion_evidence is not None and not isinstance(self.conversion_evidence, ConversionEvidence):
            raise MarginError("invalid_conversion_evidence")

    @property
    def comparison_key(self) -> tuple:
        return (self.source_id, self.source_contract_version, self.schema_version,
                self.parser_version, self.identity_evidence_version, self.scope,
                self.formula_version, self.promotion_policy_version,
                None if self.conversion_evidence is None else (
                    self.conversion_evidence.source_unit, self.conversion_evidence.target_unit,
                    self.conversion_evidence.factor, self.conversion_evidence.evidence_version))


@dataclass(frozen=True, kw_only=True)
class MarginDailyReport:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    financing: FinancingLedger
    short_margin: ShortMarginLedger
    timing: SourceTiming
    lineage: MarginSourceLineage
    quotas: tuple[QuotaLimit, ...] = ()

    def __post_init__(self):
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        _date(self.trade_date, "trade_date")
        if not isinstance(self.financing, FinancingLedger) or not isinstance(self.short_margin, ShortMarginLedger):
            raise MarginError("invalid_margin_ledger")
        if self.financing.quantity_unit is not self.short_margin.quantity_unit:
            raise MarginError("incompatible_quantity_units")
        if not isinstance(self.timing, SourceTiming) or not isinstance(self.lineage, MarginSourceLineage):
            raise MarginError("invalid_report_metadata")
        if self.timing.trade_date != self.trade_date or self.timing.date_evidence is DateEvidence.UNVERIFIED:
            raise MarginError("unverified_source_date")
        conversion = self.lineage.conversion_evidence
        if conversion is not None and (
                conversion.instrument_id != self.instrument_id or conversion.market_id != self.market_id
                or conversion.target_unit is not self.quantity_unit
                or not conversion.valid_from <= self.trade_date <= conversion.valid_through):
            raise MarginError("incompatible_report_conversion_evidence")
        if (self.timing.date_evidence_payload_sha256 is not None
                and self.timing.date_evidence_payload_sha256 != self.lineage.payload_sha256):
            raise MarginError("date_evidence_payload_mismatch")
        if self.trade_date > self.timing.first_seen_at.astimezone(ZoneInfo(self.timing.timezone)).date():
            raise MarginError("future_source_date")
        if not isinstance(self.quotas, (tuple, list)) or not all(isinstance(v, QuotaLimit) for v in self.quotas):
            raise MarginError("invalid_quota_limits")
        object.__setattr__(self, "quotas", tuple(self.quotas))
        if len({quota.ledger for quota in self.quotas}) != len(self.quotas):
            raise MarginError("duplicate_quota_ledger")
        if any(quota.quantity.unit is not self.quantity_unit or quota.report_date != self.trade_date for quota in self.quotas):
            raise MarginError("incompatible_quota_report")

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.financing.quantity_unit

    @property
    def comparison_key(self) -> tuple:
        return (self.instrument_id, self.market_id, self.quantity_unit, *self.lineage.comparison_key,
                self.financing.formula_policy, self.short_margin.formula_policy)

    def ledger(self, kind: LedgerKind) -> FinancingLedger | ShortMarginLedger:
        _enum(kind, LedgerKind, "ledger_kind")
        return self.financing if kind is LedgerKind.FINANCING else self.short_margin

    def quota(self, kind: LedgerKind) -> QuotaLimit | None:
        _enum(kind, LedgerKind, "ledger_kind")
        return next((quota for quota in self.quotas if quota.ledger is kind), None)


@dataclass(frozen=True, kw_only=True)
class MarginHistory:
    reports: tuple[MarginDailyReport, ...]

    def __post_init__(self):
        if not isinstance(self.reports, (tuple, list)) or not all(isinstance(r, MarginDailyReport) for r in self.reports):
            raise MarginError("invalid_margin_history")
        reports = tuple(sorted(self.reports, key=lambda report: report.trade_date))
        object.__setattr__(self, "reports", reports)
        if len({report.trade_date for report in reports}) != len(reports):
            raise MarginError("duplicate_report_date")
        if reports and any(report.comparison_key != reports[0].comparison_key for report in reports):
            raise MarginError("incompatible_margin_history")
