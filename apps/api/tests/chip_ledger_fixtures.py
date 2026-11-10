"""SYNTHETIC fixed ledger inputs for chip-engine tests; never production seeds."""
from datetime import date, datetime, timezone
from uuid import UUID, uuid5

from stockapp.modules.margin import public as margin_domain
from stockapp.modules.lending import public as lending_domain

INSTRUMENT = UUID(int=1)
MARKET = UUID(int=2)
SOURCE = UUID(int=3)
SEEN = datetime(2026, 2, 1, tzinfo=timezone.utc)
_DEFAULT_BALANCE = object()


def margin_quantity(value, *, availability=None, unit=margin_domain.QuantityUnit.SHARES):
    if availability is None:
        availability = (margin_domain.Availability.MISSING if value is None
                        else margin_domain.Availability.REPORTED)
    return margin_domain.Quantity(value=value, unit=unit, availability=availability)


def lending_quantity(value, *, availability=None, unit=lending_domain.QuantityUnit.SHARES):
    if availability is None:
        availability = (lending_domain.Availability.MISSING if value is None
                        else lending_domain.Availability.REPORTED)
    return lending_domain.Quantity(value=value, unit=unit, availability=availability)


def margin(day: date, previous=100, balance=110, *, revision_id=None,
           source_id=SOURCE, seen_at=SEEN, published_at=None):
    """Valid financing identity for positive/negative/zero requested changes.

    Missing previous/balance values remain missing. The separate short-margin
    ledger is fixed at 50 + 5 - 2 = 53 and never uses the financing formula.
    """
    q = margin_quantity
    diff = balance - previous if type(previous) is int and type(balance) is int else 0
    policy = margin_domain.FormulaPolicy(version='synthetic-margin-formula-v1',
        verified=True, adjustment_treatment=margin_domain.AdjustmentTreatment.NONE)
    inapplicable = q(None, availability=margin_domain.Availability.NOT_APPLICABLE)
    return margin_domain.MarginDailyReport(
        instrument_id=INSTRUMENT, market_id=MARKET, trade_date=day,
        financing=margin_domain.FinancingLedger(previous_balance=q(previous),
            purchase=q(max(diff, 0)), sale=q(max(-diff, 0)), cash_repayment=q(0),
            balance=q(balance), signed_adjustment=inapplicable, formula_policy=policy),
        short_margin=margin_domain.ShortMarginLedger(previous_balance=q(50),
            short_sell=q(5), short_cover=q(2), stock_repayment=q(0), balance=q(53),
            signed_adjustment=inapplicable, formula_policy=policy),
        timing=margin_domain.SourceTiming(trade_date=day,
            date_evidence=margin_domain.DateEvidence.SOURCE_FIELD,
            date_evidence_version='synthetic-margin-date-v1',
            first_seen_at=seen_at, source_published_at=published_at),
        lineage=margin_domain.MarginSourceLineage(source_id=source_id,
            run_id=uuid5(UUID(int=4), f'synthetic-margin-run:{day}'),
            raw_id=uuid5(UUID(int=5), f'synthetic-margin-raw:{day}'),
            revision_id=revision_id or uuid5(UUID(int=6), f'synthetic-margin-revision:{day}'),
            source_contract_version='synthetic-margin-contract-v1',
            schema_version='synthetic-margin-schema-v1', parser_version='synthetic-margin-parser-v1',
            identity_evidence_version='synthetic-dated-identity-v1', scope='synthetic-scope-v1',
            formula_version=policy.version, promotion_policy_version='synthetic-margin-promotion-v1',
            payload_sha256='a' * 64, source_row_locator='synthetic/margin/0'))


def lending(day: date, *, kind=lending_domain.ReportKind.LENDING_SHORT_SALES,
            previous=500, sold=125, returned=180, adjustment=-7,
            balance=_DEFAULT_BALANCE, revision_id=None, source_id=SOURCE,
            seen_at=SEEN, published_at=None, verified_channel=True,
            scope='synthetic-scope-v1'):
    """Default short-sale identity is 500 + 125 - 180 - 7 = 438.

    For borrowed_positions, sold is the synthetic borrowed quantity, adjustment
    is explicitly inapplicable, and the default balance is 445. An explicit
    balance, including None for a missing observation, is always retained.
    """
    q = lending_quantity
    borrowed = kind is lending_domain.ReportKind.BORROWED_POSITIONS
    if balance is _DEFAULT_BALANCE:
        balance = 445 if borrowed else 438
    policy = lending_domain.FormulaPolicy(
        version='synthetic-lending-formula-' + kind.value,
        verified=True, adjustment_treatment=(lending_domain.AdjustmentTreatment.NONE
            if borrowed else lending_domain.AdjustmentTreatment.SIGNED))
    if borrowed:
        ledger = lending_domain.BorrowingLedger(previous_balance=q(previous),
            borrowed_quantity=q(sold), returned_quantity=q(returned), balance=q(balance),
            signed_adjustment=q(None, availability=lending_domain.Availability.NOT_APPLICABLE),
            formula_policy=policy)
    else:
        ledger = lending_domain.LendingShortLedger(previous_balance=q(previous),
            short_sales=q(sold), short_returns=q(returned), signed_adjustment=q(adjustment),
            balance=q(balance), formula_policy=policy)
    channel = lending_domain.ChannelDefinition(
        channel=(lending_domain.LendingChannel.COMBINED if verified_channel
                 else lending_domain.LendingChannel.UNSPECIFIED),
        code='SLBNLB' if verified_channel else 'unmapped',
        family='synthetic-lending-family-v1', version='synthetic-lending-channel-v1',
        verified=verified_channel,
        coverage=frozenset({'synthetic-slb', 'synthetic-nlb'}) if verified_channel else frozenset())
    return lending_domain.LendingDailyReport(instrument_id=INSTRUMENT,
        market_id=MARKET, trade_date=day, report_kind=kind, ledger=ledger, channel=channel,
        timing=lending_domain.SourceTiming(trade_date=day,
            date_evidence=lending_domain.DateEvidence.SOURCE_FIELD,
            date_evidence_version='synthetic-lending-date-v1', first_seen_at=seen_at,
            source_published_at=published_at),
        lineage=lending_domain.LendingSourceLineage(source_id=source_id,
            run_id=uuid5(UUID(int=4), f'synthetic-lending-run:{day}'),
            raw_id=uuid5(UUID(int=5), f'synthetic-lending-raw:{day}'),
            revision_id=revision_id or uuid5(UUID(int=6), f'synthetic-lending-revision:{day}'),
            source_contract_version='synthetic-lending-contract-v1',
            schema_version='synthetic-lending-schema-v1', parser_version='synthetic-lending-parser-v1',
            taxonomy_version='synthetic-lending-taxonomy-v1',
            identity_evidence_version='synthetic-dated-identity-v1', scope=scope,
            formula_version=policy.version, promotion_policy_version='synthetic-lending-promotion-v1',
            payload_sha256='b' * 64, source_row_locator='synthetic/lending/0'))
