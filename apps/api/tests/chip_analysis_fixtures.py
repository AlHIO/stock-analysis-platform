"""SYNTHETIC chip-analysis inputs; never genuine securities data or DB seeds.

The calendar and bucket scheme below are authoritative only inside tests. The
default observation time intentionally follows the 2025/January-2026 test dates;
publication remains unknown unless a test explicitly supplies its own evidence.
"""
from datetime import date, datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from stockapp.modules.institutional.public import (
    CalendarSnapshot, Category, FactLineage, InstitutionalDailyReport,
    InstitutionalHistory, InvestorFlow, QuantityUnit, SourceTiming, TradeBasis, TradeScope,
)
from stockapp.modules.ownership.public import (
    AdjustmentConvention, BucketDefinition, BucketKind, BucketScheme, HoldingBucket,
    OwnershipSourceLineage, OwnershipWeeklyReport,
)

INSTRUMENT, MARKET, SOURCE = UUID(int=1), UUID(int=2), UUID(int=3)
SEEN = datetime(2026, 2, 1, tzinfo=timezone.utc)
_TAIPEI = ZoneInfo('Asia/Taipei')
_SCOPE = TradeScope(code='synthetic-all-original', includes_block_trades=True,
    includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL)
_SCHEME = BucketScheme(
    version='synthetic-chip-buckets-v1',
    source_contract_version='synthetic-chip-ownership-v1',
    adjustment_convention=AdjustmentConvention.SIGNED_DELTA,
    definitions=(
        BucketDefinition(code='small', kind=BucketKind.REGULAR, label='SYNTHETIC 1–400000',
            lower_inclusive=1, upper_inclusive=400000),
        BucketDefinition(code='medium', kind=BucketKind.REGULAR, label='SYNTHETIC 400001–1000000',
            lower_inclusive=400001, upper_inclusive=1000000),
        BucketDefinition(code='large', kind=BucketKind.REGULAR, label='SYNTHETIC 1000001+',
            lower_inclusive=1000001, upper_inclusive=None),
        BucketDefinition(code='adjustment', kind=BucketKind.ADJUSTMENT, label='SYNTHETIC adjustment'),
        BucketDefinition(code='total', kind=BucketKind.TOTAL, label='SYNTHETIC official total'),
    ),
)


def _revision(day, revision_id, offset):
    return revision_id if revision_id is not None else UUID(int=offset + day.toordinal())


def institution(day: date, net: int = 10, *, source_id=SOURCE, revision_id=None,
                published_at=None, seen_at=SEEN, components=None) -> InstitutionalDailyReport:
    """Complete disjoint synthetic leaves, or explicit caller-supplied components."""
    if components is None:
        components = tuple(InvestorFlow(category=category,
            buy_quantity=max(value, 0), sell_quantity=max(-value, 0), net_quantity=value)
            for category, value in ((Category.FOREIGN_EX_DEALERS, net),
                (Category.INVESTMENT_TRUST, 0), (Category.DEALER_PROPRIETARY, 0),
                (Category.DEALER_HEDGING, 0)))
    else:
        components = tuple(components)
    revision = _revision(day, revision_id, 1000000)
    return InstitutionalDailyReport(
        instrument_id=INSTRUMENT, market_id=MARKET, trade_date=day,
        quantity_unit=QuantityUnit.SHARES, scope=_SCOPE, components=components,
        lineage=FactLineage(source_id=source_id, source_contract_version='synthetic-chip-institutional-v1',
            parser_version='synthetic-chip-parser-v1', revision_id=revision,
            run_id=UUID(int=2000000 + day.toordinal()), raw_id=UUID(int=3000000 + day.toordinal()),
            source_row_locator=f'synthetic/institutional/{day.isoformat()}',
            payload_hash=sha256(repr((day, source_id, components)).encode()).hexdigest(),
            promotion_policy_version='synthetic-chip-promotion-v1'),
        timing=SourceTiming(first_seen_at=seen_at, last_seen_at=seen_at, ingested_at=seen_at,
            source_published_at=published_at,
            source_published_on=published_at.astimezone(_TAIPEI).date() if published_at is not None else None),
    )


def institution_history(days: tuple[date, ...], nets: tuple[int, ...], *, missing=(),
                        verified=True, history_start_verified=True,
                        source_ids=None) -> InstitutionalHistory:
    """Caller-confirmed synthetic sessions; missing rows stay expected sessions."""
    if len(days) != len(nets) or source_ids is not None and len(source_ids) != len(days):
        raise ValueError('Synthetic fixture dates, values and sources must have equal length')
    sources = (SOURCE,) * len(days) if source_ids is None else source_ids
    reports = tuple(institution(day, net, source_id=source)
        for day, net, source in zip(days, nets, sources, strict=True) if day not in missing)
    return InstitutionalHistory(instrument_id=INSTRUMENT, market_id=MARKET,
        calendar=CalendarSnapshot(market_id=MARKET, start=days[0] if days else date(2026, 1, 1),
            end=days[-1] if days else date(2026, 1, 1), session_dates=days, verified=verified,
            version='synthetic-chip-session-calendar-v1'),
        reports=reports, history_start_verified=history_start_verified)


def ownership(day: date, high_quantity=7000000, *, total=10000000, revision_id=None,
              source_id=SOURCE, published_at=None, seen_at=SEEN) -> OwnershipWeeklyReport:
    """Synthetic distribution with exact 400001/1000001 bucket boundaries."""
    small_quantity = total - high_quantity
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        high_percent = (Decimal(high_quantity) * 100 / Decimal(total)).quantize(
            Decimal('0.000000000001')) if total else Decimal(0)
        small_percent = Decimal(100) - high_percent if total else Decimal(0)
    small_holders = 10 if small_quantity else 0
    large_holders = 1 if high_quantity else 0
    buckets = (
        HoldingBucket(code='small', kind=BucketKind.REGULAR, holder_count=small_holders,
            quantity=small_quantity, reported_percent=small_percent),
        HoldingBucket(code='medium', kind=BucketKind.REGULAR, holder_count=0,
            quantity=0, reported_percent=Decimal(0)),
        HoldingBucket(code='large', kind=BucketKind.REGULAR, holder_count=large_holders,
            quantity=high_quantity, reported_percent=high_percent),
        HoldingBucket(code='adjustment', kind=BucketKind.ADJUSTMENT, holder_count=0,
            quantity=0, reported_percent=Decimal(0)),
        HoldingBucket(code='total', kind=BucketKind.TOTAL, holder_count=small_holders + large_holders,
            quantity=total, reported_percent=Decimal(100)),
    )
    revision = _revision(day, revision_id, 4000000)
    return OwnershipWeeklyReport(instrument_id=INSTRUMENT, market_id=MARKET, as_of_date=day,
        quantity_unit='shares', scheme=_SCHEME, buckets=buckets,
        lineage=OwnershipSourceLineage(source_id=source_id, run_id=UUID(int=5000000 + day.toordinal()),
            raw_id=UUID(int=6000000 + day.toordinal()), revision_id=revision,
            source_contract_version=_SCHEME.source_contract_version, parser_version='synthetic-chip-parser-v1',
            source_row_locator=f'synthetic/ownership/{day.isoformat()}',
            identity_evidence_version='synthetic-chip-reviewed-v1',
            promotion_policy_version='synthetic-chip-promotion-v1',
            payload_hash=sha256(repr((day, source_id, high_quantity, total)).encode()).hexdigest()),
        first_seen_at=seen_at, last_seen_at=seen_at, ingested_at=seen_at,
        source_published_at=published_at,
        source_published_on=published_at.astimezone(_TAIPEI).date() if published_at is not None else None)
