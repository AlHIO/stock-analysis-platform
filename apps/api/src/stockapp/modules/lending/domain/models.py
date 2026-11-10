"""Immutable lending facts; balances, flows, quotas and channels stay distinct."""
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


class LendingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value, name: str):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 256 or "\x00" in value):
        raise LendingError("invalid_" + name)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise LendingError("invalid_" + name) from None


def _date(value, name: str):
    if type(value) is not date:
        raise LendingError("invalid_" + name)


def _aware(value, name: str):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise LendingError("invalid_" + name)


def _uuid(value, name: str):
    if not isinstance(value, UUID):
        raise LendingError("invalid_" + name)


def _enum(value, cls, name: str):
    if not isinstance(value, cls):
        raise LendingError("invalid_" + name)


def _coverage(value, name: str) -> frozenset[str]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise LendingError("invalid_" + name)
    for member in value:
        _text(member, name)
    if len(set(value)) != len(value):
        raise LendingError("duplicate_" + name)
    return frozenset(value)


class QuantityUnit(StrEnum):
    SHARES = "shares"
    UNITS = "units"
    LOTS = "lots"


class Availability(StrEnum):
    REPORTED = "reported"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, kw_only=True)
class Quantity:
    """Signed only as a value object; business roles restrict signs."""
    value: int | None
    unit: QuantityUnit
    availability: Availability

    def __post_init__(self):
        _enum(self.unit, QuantityUnit, "quantity_unit")
        _enum(self.availability, Availability, "availability")
        if self.availability is Availability.REPORTED:
            if type(self.value) is not int or not BIGINT_MIN <= self.value <= BIGINT_MAX:
                raise LendingError("invalid_quantity")
        elif self.value is not None:
            raise LendingError("unavailable_quantity_has_value")


class AdjustmentTreatment(StrEnum):
    NONE = "none"
    SIGNED = "signed"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, kw_only=True)
class FormulaPolicy:
    version: str
    verified: bool
    adjustment_treatment: AdjustmentTreatment

    def __post_init__(self):
        _text(self.version, "formula_policy_version")
        if type(self.verified) is not bool:
            raise LendingError("invalid_formula_verification")
        _enum(self.adjustment_treatment, AdjustmentTreatment, "adjustment_treatment")


def _ledger(values: tuple[Quantity, ...], adjustment: Quantity, policy: FormulaPolicy):
    if not all(isinstance(value, Quantity) for value in (*values, adjustment)):
        raise LendingError("invalid_ledger_quantity")
    if len({value.unit for value in (*values, adjustment)}) != 1:
        raise LendingError("incompatible_quantity_units")
    if any(value.value is not None and value.value < 0 for value in values):
        raise LendingError("negative_balance_or_flow")
    if not isinstance(policy, FormulaPolicy):
        raise LendingError("invalid_formula_policy")
    if (policy.adjustment_treatment is AdjustmentTreatment.NONE
            and adjustment.availability is not Availability.NOT_APPLICABLE):
        raise LendingError("unexpected_adjustment")


@dataclass(frozen=True, kw_only=True)
class BorrowingLedger:
    """Borrowing outstanding and new borrowing/returns, not short selling."""
    previous_balance: Quantity
    borrowed_quantity: Quantity
    returned_quantity: Quantity
    balance: Quantity
    signed_adjustment: Quantity
    formula_policy: FormulaPolicy

    def __post_init__(self):
        _ledger((self.previous_balance, self.borrowed_quantity,
                 self.returned_quantity, self.balance), self.signed_adjustment,
                self.formula_policy)

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.balance.unit


@dataclass(frozen=True, kw_only=True)
class LendingShortLedger:
    """Securities lending short-sale outstanding and its separate daily flows."""
    previous_balance: Quantity
    short_sales: Quantity
    short_returns: Quantity
    signed_adjustment: Quantity
    balance: Quantity
    formula_policy: FormulaPolicy

    def __post_init__(self):
        _ledger((self.previous_balance, self.short_sales, self.short_returns,
                 self.balance), self.signed_adjustment, self.formula_policy)

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.balance.unit


class ReportKind(StrEnum):
    BORROWED_POSITIONS = "borrowed_positions"
    LENDING_SHORT_SALES = "lending_short_sales"


class LendingChannel(StrEnum):
    COMBINED = "combined"
    SLB = "slb"
    NLB = "nlb"
    OTHER_VERIFIED = "other_verified"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True, kw_only=True)
class ChannelDefinition:
    """Channel identity is separate from proven decomposition of its coverage.

    A verified SLBNLB label may have unknown leaf coverage. An empty coverage
    set never proves disjointness or completeness for an aggregate.
    """
    channel: LendingChannel
    code: str
    family: str
    version: str
    verified: bool
    coverage: frozenset[str] = frozenset()

    def __post_init__(self):
        _enum(self.channel, LendingChannel, "lending_channel")
        for name in ("code", "family", "version"):
            _text(getattr(self, name), "channel_" + name)
        if type(self.verified) is not bool:
            raise LendingError("invalid_channel_verification")
        object.__setattr__(self, "coverage", _coverage(self.coverage, "channel_coverage"))
        expected = {LendingChannel.COMBINED:"SLBNLB", LendingChannel.SLB:"SLB",
                    LendingChannel.NLB:"NLB"}
        if self.channel in expected and self.code != expected[self.channel]:
            raise LendingError("incompatible_channel_code")
        if self.channel is LendingChannel.UNSPECIFIED:
            if self.verified or self.coverage:
                raise LendingError("unspecified_channel_cannot_claim_coverage")
        elif self.channel is LendingChannel.OTHER_VERIFIED:
            if not self.verified or self.code in {"SLBNLB", "SLB", "NLB"}:
                raise LendingError("invalid_other_channel")
        if self.coverage and not self.verified:
            raise LendingError("unverified_channel_cannot_claim_coverage")

    @property
    def identity_key(self) -> tuple:
        return self.family, self.code


@dataclass(frozen=True, kw_only=True)
class ChannelPartition:
    """Reviewed exact partition, never inferred from two channel names."""
    family: str
    version: str
    total_code: str
    member_codes: tuple[str, ...]
    total_coverage: frozenset[str]
    verified: bool
    evidence_version: str
    source_id: UUID
    source_contract_version: str
    taxonomy_version: str
    report_kind: ReportKind
    scope: str
    quantity_unit: QuantityUnit
    valid_from: date
    valid_through: date

    def __post_init__(self):
        for name in ("family", "version", "total_code", "evidence_version",
                     "source_contract_version", "taxonomy_version", "scope"):
            _text(getattr(self, name), "partition_" + name)
        _uuid(self.source_id, "partition_source_id")
        _enum(self.report_kind, ReportKind, "partition_report_kind")
        _enum(self.quantity_unit, QuantityUnit, "partition_quantity_unit")
        if self.quantity_unit not in {QuantityUnit.SHARES, QuantityUnit.UNITS}:
            raise LendingError("unsupported_partition_unit")
        if type(self.verified) is not bool:
            raise LendingError("invalid_partition_verification")
        if not isinstance(self.member_codes, (tuple, list)):
            raise LendingError("invalid_partition_members")
        for code in self.member_codes:
            _text(code, "partition_member")
        members = tuple(self.member_codes)
        if (len(members) < 2 or len(set(members)) != len(members)
                or self.total_code in members):
            raise LendingError("invalid_partition_members")
        object.__setattr__(self, "member_codes", members)
        object.__setattr__(self, "total_coverage", _coverage(
            self.total_coverage, "partition_coverage"))
        if self.verified and not self.total_coverage:
            raise LendingError("missing_verified_partition_coverage")
        _date(self.valid_from, "partition_valid_from")
        _date(self.valid_through, "partition_valid_through")
        if self.valid_from > self.valid_through:
            raise LendingError("invalid_partition_effective_range")


class DateEvidence(StrEnum):
    SOURCE_FIELD = "source_field"
    VERIFIED_PAYLOAD = "verified_payload"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, kw_only=True)
class SourceTiming:
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
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise LendingError("invalid_timezone") from None
        if self.trade_date is not None:
            _date(self.trade_date, "trade_date")
        if self.date_evidence_version is not None:
            _text(self.date_evidence_version, "date_evidence_version")
        if self.date_evidence is not DateEvidence.UNVERIFIED and (
                self.trade_date is None or self.date_evidence_version is None):
            raise LendingError("missing_trusted_source_date")
        if self.date_evidence_payload_sha256 is not None and (
                not isinstance(self.date_evidence_payload_sha256, str)
                or re.fullmatch("[0-9a-f]{64}", self.date_evidence_payload_sha256) is None):
            raise LendingError("invalid_date_evidence_payload_hash")
        if (self.date_evidence is DateEvidence.VERIFIED_PAYLOAD
                and self.date_evidence_payload_sha256 is None):
            raise LendingError("missing_date_evidence_payload_hash")
        if self.source_published_at is not None:
            _aware(self.source_published_at, "source_published_at")
            if self.source_published_at > self.first_seen_at:
                raise LendingError("publication_after_first_seen")
        if self.source_published_on is not None:
            _date(self.source_published_on, "source_published_on")
            if self.source_published_on > self.first_seen_at.astimezone(zone).date():
                raise LendingError("publication_after_first_seen")
            if (self.source_published_at is not None
                    and self.source_published_on != self.source_published_at.astimezone(zone).date()):
                raise LendingError("inconsistent_publication_date")


class EffectiveRelation(StrEnum):
    SAME_DAY = "same_day"
    NEXT_BUSINESS_DAY = "next_business_day"
    UNSPECIFIED = "unspecified"


class QuotaPurpose(StrEnum):
    BORROWING = "borrowing"
    SHORT_SALE = "short_sale"
    AVAILABLE_SHORT_SALE = "available_short_sale"


@dataclass(frozen=True, kw_only=True)
class RegulatoryQuota:
    """A source regulatory limit/availability statement, never actual volume."""
    purpose: QuotaPurpose
    quantity: Quantity
    report_date: date | None
    effective_on: date | None
    effective_relation: EffectiveRelation
    basis_code: str
    evidence_version: str | None

    def __post_init__(self):
        _enum(self.purpose, QuotaPurpose, "quota_purpose")
        _enum(self.effective_relation, EffectiveRelation, "effective_relation")
        _text(self.basis_code, "quota_basis")
        if not isinstance(self.quantity, Quantity):
            raise LendingError("invalid_quota_quantity")
        if self.quantity.value is not None and self.quantity.value < 0:
            raise LendingError("negative_quota")
        if self.report_date is not None:
            _date(self.report_date, "quota_report_date")
        if self.effective_on is not None:
            _date(self.effective_on, "quota_effective_on")
            if self.report_date is not None:
                if self.effective_on < self.report_date:
                    raise LendingError("invalid_quota_effective_on")
                if (self.effective_relation is EffectiveRelation.SAME_DAY
                        and self.effective_on != self.report_date):
                    raise LendingError("incompatible_quota_effectivity")
                if (self.effective_relation is EffectiveRelation.NEXT_BUSINESS_DAY
                        and self.effective_on <= self.report_date):
                    raise LendingError("incompatible_quota_effectivity")
        if self.evidence_version is not None:
            _text(self.evidence_version, "quota_evidence_version")


@dataclass(frozen=True, kw_only=True)
class LendingSourceLineage:
    source_id: UUID
    run_id: UUID
    raw_id: UUID
    revision_id: UUID
    source_contract_version: str
    schema_version: str
    parser_version: str
    taxonomy_version: str
    identity_evidence_version: str
    scope: str
    formula_version: str
    promotion_policy_version: str
    payload_sha256: str
    source_row_locator: str

    def __post_init__(self):
        for name in ("source_id", "run_id", "raw_id", "revision_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_contract_version", "schema_version", "parser_version",
                     "taxonomy_version", "identity_evidence_version", "scope",
                     "formula_version", "promotion_policy_version", "source_row_locator"):
            _text(getattr(self, name), name)
        if (not isinstance(self.payload_sha256, str)
                or re.fullmatch("[0-9a-f]{64}", self.payload_sha256) is None):
            raise LendingError("invalid_payload_sha256")

    @property
    def comparison_key(self) -> tuple:
        return (self.source_id, self.source_contract_version, self.schema_version,
                self.parser_version, self.taxonomy_version, self.identity_evidence_version,
                self.scope, self.formula_version, self.promotion_policy_version)


@dataclass(frozen=True, kw_only=True)
class LendingDailyReport:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    report_kind: ReportKind
    ledger: BorrowingLedger | LendingShortLedger
    channel: ChannelDefinition
    timing: SourceTiming
    lineage: LendingSourceLineage
    quotas: tuple[RegulatoryQuota, ...] = ()

    def __post_init__(self):
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        _date(self.trade_date, "trade_date")
        _enum(self.report_kind, ReportKind, "report_kind")
        expected = (BorrowingLedger if self.report_kind is ReportKind.BORROWED_POSITIONS
                    else LendingShortLedger)
        if not isinstance(self.ledger, expected):
            raise LendingError("incompatible_report_ledger")
        if self.ledger.quantity_unit not in {QuantityUnit.SHARES, QuantityUnit.UNITS}:
            raise LendingError("canonical_unit_conversion_required")
        if not isinstance(self.channel, ChannelDefinition):
            raise LendingError("invalid_report_channel")
        if not isinstance(self.timing, SourceTiming) or not isinstance(self.lineage, LendingSourceLineage):
            raise LendingError("invalid_report_metadata")
        if (self.timing.trade_date != self.trade_date
                or self.timing.date_evidence is DateEvidence.UNVERIFIED):
            raise LendingError("unverified_source_date")
        if (self.timing.date_evidence_payload_sha256 is not None
                and self.timing.date_evidence_payload_sha256 != self.lineage.payload_sha256):
            raise LendingError("date_evidence_payload_mismatch")
        if self.trade_date > self.timing.first_seen_at.astimezone(ZoneInfo(self.timing.timezone)).date():
            raise LendingError("future_source_date")
        if not isinstance(self.quotas, (tuple, list)) or not all(
                isinstance(value, RegulatoryQuota) for value in self.quotas):
            raise LendingError("invalid_report_quotas")
        object.__setattr__(self, "quotas", tuple(self.quotas))
        if len({q.purpose for q in self.quotas}) != len(self.quotas):
            raise LendingError("duplicate_quota_purpose")
        allowed = ({QuotaPurpose.BORROWING} if self.report_kind is ReportKind.BORROWED_POSITIONS
                   else {QuotaPurpose.SHORT_SALE, QuotaPurpose.AVAILABLE_SHORT_SALE})
        if any(q.purpose not in allowed or q.report_date != self.trade_date
               or q.quantity.unit is not self.quantity_unit for q in self.quotas):
            raise LendingError("incompatible_report_quota")

    @property
    def quantity_unit(self) -> QuantityUnit:
        return self.ledger.quantity_unit

    @property
    def natural_key(self) -> tuple:
        return (self.instrument_id, self.trade_date, self.report_kind,
                self.lineage.scope, *self.channel.identity_key)

    @property
    def series_key(self) -> tuple:
        """Comparable economic profile excluding channel identity."""
        return (self.instrument_id, self.market_id, self.report_kind, self.quantity_unit,
                *self.lineage.comparison_key, self.ledger.formula_policy,
                self.channel.family, self.channel.version)

    @property
    def comparison_key(self) -> tuple:
        return (*self.series_key, self.channel)


@dataclass(frozen=True, kw_only=True)
class LendingHistory:
    """One comparable product/profile; multiple explicit channels may coexist."""
    reports: tuple[LendingDailyReport, ...]

    def __post_init__(self):
        if not isinstance(self.reports, (tuple, list)) or not all(
                isinstance(report, LendingDailyReport) for report in self.reports):
            raise LendingError("invalid_lending_history")
        reports = tuple(sorted(self.reports, key=lambda report:(
            report.trade_date, report.channel.family, report.channel.code)))
        object.__setattr__(self, "reports", reports)
        if len({report.natural_key for report in reports}) != len(reports):
            raise LendingError("duplicate_report_key")
        if reports and any(report.series_key != reports[0].series_key for report in reports):
            raise LendingError("incompatible_lending_history")
        definitions = {}
        for report in reports:
            prior = definitions.setdefault(report.channel.identity_key, report.channel)
            if prior != report.channel:
                raise LendingError("incompatible_channel_definition")
