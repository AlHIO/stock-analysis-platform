"""Exact, versioned ownership snapshots; totals and adjustments remain distinct."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


class OwnershipError(ValueError):
    def __init__(self, code: str, raw=None):
        self.code, self.raw = code, raw
        super().__init__(code)


def _text(value, field):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise OwnershipError('invalid_' + field)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise OwnershipError('invalid_' + field) from None
    if '\x00' in value:
        raise OwnershipError('invalid_' + field)


def _uuid(value, field):
    if not isinstance(value, UUID):
        raise OwnershipError('invalid_' + field)


def _date(value, field):
    if type(value) is not date:
        raise OwnershipError('invalid_' + field)


def _aware(value, field):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise OwnershipError('invalid_' + field)


def _quantity(value, field, *, signed=False):
    if value is not None and (type(value) is not int or not (BIGINT_MIN if signed else 0) <= value <= BIGINT_MAX):
        raise OwnershipError('invalid_' + field)


class BucketKind(StrEnum):
    REGULAR = 'regular'
    ADJUSTMENT = 'adjustment'
    TOTAL = 'total'


class AdjustmentConvention(StrEnum):
    SHORTFALL_MAGNITUDE = 'shortfall_magnitude'
    SIGNED_DELTA = 'signed_delta'
    UNVERIFIED = 'unverified'


class OwnershipCompleteness(StrEnum):
    COMPLETE = 'complete'
    PARTIAL = 'partial'
    NOT_VERIFIED = 'not_verified'


@dataclass(frozen=True, kw_only=True)
class BucketDefinition:
    code: str
    kind: BucketKind
    label: str
    lower_inclusive: int | None = None
    upper_inclusive: int | None = None

    def __post_init__(self):
        _text(self.code, 'bucket_code')
        _text(self.label, 'bucket_label')
        if not isinstance(self.kind, BucketKind):
            raise OwnershipError('invalid_bucket_kind')
        if self.kind is BucketKind.REGULAR:
            if type(self.lower_inclusive) is not int or not 1 <= self.lower_inclusive <= BIGINT_MAX:
                raise OwnershipError('invalid_bucket_bounds')
            if self.upper_inclusive is not None and (type(self.upper_inclusive) is not int
                    or not self.lower_inclusive <= self.upper_inclusive <= BIGINT_MAX):
                raise OwnershipError('invalid_bucket_bounds')
        elif self.lower_inclusive is not None or self.upper_inclusive is not None:
            raise OwnershipError('nonregular_bucket_bounds')


@dataclass(frozen=True, kw_only=True)
class BucketScheme:
    version: str
    definitions: tuple[BucketDefinition, ...]
    source_contract_version: str
    adjustment_convention: AdjustmentConvention

    def __post_init__(self):
        _text(self.version, 'scheme_version')
        _text(self.source_contract_version, 'source_contract_version')
        if not isinstance(self.adjustment_convention, AdjustmentConvention):
            raise OwnershipError('invalid_adjustment_convention')
        if not isinstance(self.definitions, (tuple, list)) or not all(isinstance(v, BucketDefinition) for v in self.definitions):
            raise OwnershipError('invalid_bucket_definitions')
        object.__setattr__(self, 'definitions', tuple(self.definitions))
        if not 2 <= len(self.definitions) <= 64 or len({v.code for v in self.definitions}) != len(self.definitions):
            raise OwnershipError('duplicate_or_invalid_bucket_definitions')
        regular = [v for v in self.definitions if v.kind is BucketKind.REGULAR]
        if not regular or sum(v.kind is BucketKind.TOTAL for v in self.definitions) != 1:
            raise OwnershipError('invalid_bucket_scheme')
        if sum(v.kind is BucketKind.ADJUSTMENT for v in self.definitions) > 1:
            raise OwnershipError('invalid_bucket_scheme')
        previous = None
        for index, definition in enumerate(regular):
            if index and (previous is None or definition.lower_inclusive != previous + 1):
                raise OwnershipError('overlapping_or_gapped_bucket_bounds')
            if not index and definition.lower_inclusive != 1:
                raise OwnershipError('invalid_bucket_scheme_start')
            if definition.upper_inclusive is None and index != len(regular) - 1:
                raise OwnershipError('invalid_open_bucket')
            previous = definition.upper_inclusive

    def definition(self, code):
        return next((v for v in self.definitions if v.code == code), None)

    @property
    def codes(self):
        return tuple(v.code for v in self.definitions)


_BOUNDS = ((1,999),(1000,5000),(5001,10000),(10001,15000),(15001,20000),
    (20001,30000),(30001,40000),(40001,50000),(50001,100000),(100001,200000),
    (200001,400000),(400001,600000),(600001,800000),(800001,1000000),(1000001,None))
TDCC_SCHEME = BucketScheme(version='tdcc-holding-17-v1', source_contract_version='tdcc-csv-6-v1',
    adjustment_convention=AdjustmentConvention.SHORTFALL_MAGNITUDE,
    definitions=tuple(BucketDefinition(code=str(i),kind=BucketKind.REGULAR,
        label=f'{lower}–{upper}' if upper is not None else f'{lower}以上',
        lower_inclusive=lower,upper_inclusive=upper) for i,(lower,upper) in enumerate(_BOUNDS,1)) + (
        BucketDefinition(code='16',kind=BucketKind.ADJUSTMENT,label='差異數調整'),
        BucketDefinition(code='17',kind=BucketKind.TOTAL,label='合計')))


@dataclass(frozen=True, kw_only=True)
class HoldingBucket:
    code: str
    kind: BucketKind
    holder_count: int | None
    quantity: int | None
    reported_percent: Decimal | None

    def __post_init__(self):
        _text(self.code, 'bucket_code')
        if not isinstance(self.kind, BucketKind):
            raise OwnershipError('invalid_bucket_kind')
        signed = self.kind is BucketKind.ADJUSTMENT
        _quantity(self.holder_count,'holder_count',signed=signed)
        _quantity(self.quantity,'quantity',signed=signed)
        value = self.reported_percent
        if value is not None:
            limit = Decimal('99999999.999999999999') if signed else Decimal(100)
            if not isinstance(value, Decimal) or not value.is_finite() or not (limit.copy_negate() if signed else 0) <= value <= limit:
                raise OwnershipError('invalid_reported_percent')
            with localcontext() as context:
                context.prec = 50
                if value != value.quantize(Decimal('0.000000000001')):
                    raise OwnershipError('reported_percent_precision_exceeded')


@dataclass(frozen=True, kw_only=True)
class OwnershipSourceLineage:
    source_id: UUID
    run_id: UUID
    raw_id: UUID
    revision_id: UUID
    source_contract_version: str
    parser_version: str
    source_row_locator: str
    identity_evidence_version: str = 'unverified'
    promotion_policy_version: str = 'not_applied'
    payload_hash: str = '0' * 64

    def __post_init__(self):
        for name in ('source_id','run_id','raw_id','revision_id'):
            _uuid(getattr(self,name),name)
        for name in ('source_contract_version','parser_version','source_row_locator',
                'identity_evidence_version','promotion_policy_version'):
            _text(getattr(self,name),name)
        if not isinstance(self.payload_hash,str) or len(self.payload_hash) != 64 or any(v not in '0123456789abcdef' for v in self.payload_hash):
            raise OwnershipError('invalid_payload_hash')


@dataclass(frozen=True, kw_only=True)
class OwnershipWeeklyReport:
    instrument_id: UUID
    market_id: UUID
    as_of_date: date
    quantity_unit: str
    scheme: BucketScheme
    buckets: tuple[HoldingBucket, ...]
    lineage: OwnershipSourceLineage
    first_seen_at: datetime
    last_seen_at: datetime
    ingested_at: datetime
    source_published_on: date | None = None
    source_published_at: datetime | None = None
    denominator: str = 'depository_inventory'

    def __post_init__(self):
        _uuid(self.instrument_id,'instrument_id')
        _uuid(self.market_id,'market_id')
        _date(self.as_of_date,'as_of_date')
        if self.quantity_unit not in {'shares','units'} or self.denominator != 'depository_inventory':
            raise OwnershipError('invalid_ownership_unit_or_denominator')
        if not isinstance(self.scheme,BucketScheme) or not isinstance(self.lineage,OwnershipSourceLineage):
            raise OwnershipError('invalid_ownership_profile')
        if self.scheme.source_contract_version != self.lineage.source_contract_version:
            raise OwnershipError('ownership_contract_mismatch')
        for name in ('first_seen_at','last_seen_at','ingested_at'):
            _aware(getattr(self,name),name)
        if not self.first_seen_at <= self.last_seen_at <= self.ingested_at:
            raise OwnershipError('invalid_ownership_timestamps')
        if self.as_of_date > self.first_seen_at.astimezone(ZoneInfo('Asia/Taipei')).date():
            raise OwnershipError('future_as_of_date')
        if self.source_published_on is not None:
            _date(self.source_published_on,'source_published_on')
        if self.source_published_at is not None:
            _aware(self.source_published_at,'source_published_at')
            if self.source_published_on is not None and self.source_published_at.astimezone(ZoneInfo('Asia/Taipei')).date() != self.source_published_on:
                raise OwnershipError('source_publication_date_mismatch')
        if not isinstance(self.buckets,(tuple,list)) or not all(isinstance(v,HoldingBucket) for v in self.buckets):
            raise OwnershipError('invalid_holding_buckets')
        object.__setattr__(self,'buckets',tuple(self.buckets))
        codes = [v.code for v in self.buckets]
        if len(codes) != len(set(codes)):
            raise OwnershipError('duplicate_bucket')
        for bucket in self.buckets:
            definition = self.scheme.definition(bucket.code)
            if definition is None or bucket.kind is not definition.kind:
                raise OwnershipError('unknown_or_mismatched_bucket')
            if bucket.kind is BucketKind.ADJUSTMENT and self.scheme.adjustment_convention is AdjustmentConvention.SHORTFALL_MAGNITUDE and bucket.quantity is not None and bucket.quantity < 0:
                raise OwnershipError('negative_shortfall_magnitude')
        self._reconcile()

    def bucket(self, code):
        return next((v for v in self.buckets if v.code == code),None)

    @property
    def missing_bucket_codes(self):
        return tuple(code for code in self.scheme.codes if self.bucket(code) is None)

    @property
    def source_total_holders(self):
        total = next((v for v in self.buckets if v.kind is BucketKind.TOTAL),None)
        return total.holder_count if total else None

    @property
    def source_total_quantity(self):
        total = next((v for v in self.buckets if v.kind is BucketKind.TOTAL),None)
        return total.quantity if total else None

    @property
    def completeness(self):
        if self.missing_bucket_codes or any(v.quantity is None or (v.kind is not BucketKind.ADJUSTMENT
                and (v.holder_count is None or v.reported_percent is None)) for v in self.buckets):
            return OwnershipCompleteness.PARTIAL
        if self.scheme.adjustment_convention is AdjustmentConvention.UNVERIFIED:
            return OwnershipCompleteness.NOT_VERIFIED
        return OwnershipCompleteness.COMPLETE

    def _reconcile(self):
        if self.missing_bucket_codes:
            return
        regular = [v for v in self.buckets if v.kind is BucketKind.REGULAR]
        adjustment = next((v for v in self.buckets if v.kind is BucketKind.ADJUSTMENT),None)
        if self.source_total_holders is not None and all(v.holder_count is not None for v in regular):
            if sum(v.holder_count for v in regular) != self.source_total_holders:
                raise OwnershipError('holder_total_mismatch')
        if self.source_total_quantity is None or any(v.quantity is None for v in regular) or adjustment is not None and adjustment.quantity is None:
            return
        delta = adjustment.quantity if adjustment is not None else 0
        convention = self.scheme.adjustment_convention
        if convention is AdjustmentConvention.UNVERIFIED:
            return
        expected = sum(v.quantity for v in regular) + (-delta if convention is AdjustmentConvention.SHORTFALL_MAGNITUDE else delta)
        if self.source_total_quantity != expected:
            raise OwnershipError('quantity_total_mismatch')

    @property
    def warnings(self):
        regular = [v for v in self.buckets if v.kind is BucketKind.REGULAR]
        total = next((v for v in self.buckets if v.kind is BucketKind.TOTAL),None)
        if (not self.missing_bucket_codes and total is not None and total.reported_percent is not None
                and all(v.reported_percent is not None for v in regular)):
            with localcontext() as context:
                context.prec = 50
                if sum((v.reported_percent for v in regular), Decimal(0)) != total.reported_percent:
                    return ('reported_percent_residual',)
        return ()

    @property
    def comparison_key(self):
        return (self.lineage.source_id,self.lineage.source_contract_version,self.scheme.version,
            self.scheme.adjustment_convention,self.quantity_unit,self.denominator)


def report_fingerprint(report: OwnershipWeeklyReport) -> str:
    """Stable source facts/profile hash; observation IDs and timestamps excluded."""
    def percent_text(value):
        if value is None:
            return None
        if value == 0:
            return '0'
        with localcontext() as context:
            context.prec = 50
            return format(value.normalize(),'f')
    value = dict(instrument_id=str(report.instrument_id),market_id=str(report.market_id),
        as_of_date=report.as_of_date.isoformat(),quantity_unit=report.quantity_unit,
        denominator=report.denominator,source_id=str(report.lineage.source_id),
        source_contract_version=report.lineage.source_contract_version,scheme_version=report.scheme.version,
        adjustment_convention=report.scheme.adjustment_convention.value,
        bucket_definitions=[dict(code=v.code,kind=v.kind.value,label=v.label,
            lower_inclusive=v.lower_inclusive,upper_inclusive=v.upper_inclusive) for v in report.scheme.definitions],
        buckets=[dict(code=v.code,kind=v.kind.value,holder_count=v.holder_count,quantity=v.quantity,
            reported_percent=percent_text(v.reported_percent))
            for v in sorted(report.buckets,key=lambda row:row.code)])
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
