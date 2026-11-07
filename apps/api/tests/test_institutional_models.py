"""Fixed SYNTHETIC institutional contracts; no HTTP, database or real securities.

All IDs, names, dates and quantities below are synthetic unit-test observations.
The explicit synthetic calendar is evidence supplied by the caller, not a claim
about the real exchange's business days.
"""

import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.models import (
    AggregationRole, BIGINT_MAX, BIGINT_MIN, CalendarSnapshot, Category,
    Completeness, FactLineage, InstitutionalDailyReport, InstitutionalHistory,
    InstitutionalValidationError, InvestorFlow, MappingStatus, QuantityUnit,
    SourceTiming, TradeBasis, TradeScope,
)

INSTRUMENT = UUID("00000000-0000-4000-8000-000000000101")
MARKET = UUID("00000000-0000-4000-8000-000000000201")
SOURCE = UUID("00000000-0000-4000-8000-000000000301")
DAY = date(2026, 11, 5)
SEEN = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)


def synthetic_scope(**changes):
    values = dict(code="synthetic_all_original", includes_block_trades=True,
                  includes_odd_lots=True, includes_after_hours=True,
                  basis=TradeBasis.ORIGINAL)
    return TradeScope(**(values | changes))


def synthetic_lineage(**changes):
    values = dict(source_id=SOURCE, source_contract_version="synthetic-v1",
        parser_version="synthetic-parser-v1",
        revision_id=UUID("00000000-0000-4000-8000-000000000401"),
        run_id=UUID("00000000-0000-4000-8000-000000000501"),
        raw_id=UUID("00000000-0000-4000-8000-000000000601"),
        source_row_locator="synthetic/table/0/row/0", payload_hash="a" * 64)
    return FactLineage(**(values | changes))


def synthetic_timing(**changes):
    values = dict(first_seen_at=SEEN, last_seen_at=SEEN, ingested_at=SEEN,
                  source_published_on=date(2026, 11, 6))
    return SourceTiming(**(values | changes))


def flow(category, buy=None, sell=None, net=None, **changes):
    return InvestorFlow(category=category, buy_quantity=buy, sell_quantity=sell,
                        net_quantity=net, **changes)


def synthetic_components():
    # Leaf net total: 600 - 100 - 100 + 150 = 550.
    return (
        flow(Category.FOREIGN_EX_DEALERS, 1000, 400, 600),
        flow(Category.INVESTMENT_TRUST, 200, 300, -100),
        flow(Category.DEALER_PROPRIETARY, 50, 150, -100),
        flow(Category.DEALER_HEDGING, 400, 250, 150),
        flow(Category.DEALER_TOTAL, 450, 400, 50),
        flow(Category.THREE_MAJOR_TOTAL, net=550),
        flow(Category.FOREIGN_DEALERS, 100, 120, -20),
        flow(Category.FOREIGN_INCLUSIVE, 1100, 520, 580),
    )


def synthetic_report(**changes):
    values = dict(instrument_id=INSTRUMENT, market_id=MARKET, trade_date=DAY,
        quantity_unit=QuantityUnit.SHARES, scope=synthetic_scope(),
        lineage=synthetic_lineage(), timing=synthetic_timing(),
        components=synthetic_components())
    return InstitutionalDailyReport(**(values | changes))


def synthetic_calendar(days=(DAY,), **changes):
    values = dict(market_id=MARKET, start=DAY, end=DAY + timedelta(days=3),
                  session_dates=days, verified=True, version="synthetic-calendar-v1")
    return CalendarSnapshot(**(values | changes))


def synthetic_history(reports=None, **changes):
    values = dict(instrument_id=INSTRUMENT, market_id=MARKET,
        calendar=synthetic_calendar(),
        reports=(synthetic_report(),) if reports is None else reports)
    return InstitutionalHistory(**(values | changes))


@pytest.mark.parametrize("buy,sell,expected", [(1000, 400, 600), (7, 19, -12),
    (0, 0, 0), (BIGINT_MAX, BIGINT_MAX - 37, 37)])
def test_net_is_exact_buy_minus_sell_without_float_rounding(buy, sell, expected):
    value = flow(Category.FOREIGN_EX_DEALERS, buy, sell)
    assert value.resolved_net == expected
    assert type(value.resolved_net) is int
    assert value.net_quantity is None  # Derived net never rewrites source data.


@pytest.mark.parametrize("field", ["buy_quantity", "sell_quantity", "net_quantity"])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", Decimal("1"),
    Decimal("1.5"), Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")])
def test_base_quantities_require_actual_integers(field, invalid):
    with pytest.raises(InstitutionalValidationError) as error:
        InvestorFlow(category=Category.FOREIGN_EX_DEALERS, **{field: invalid})
    assert error.value.code == "invalid_quantity"


@pytest.mark.parametrize("field,value", [("buy_quantity", -1), ("sell_quantity", -1),
    ("buy_quantity", BIGINT_MAX + 1), ("sell_quantity", BIGINT_MAX + 1),
    ("net_quantity", BIGINT_MAX + 1), ("net_quantity", BIGINT_MIN - 1)])
def test_source_quantity_bigint_bounds_are_enforced(field, value):
    with pytest.raises(InstitutionalValidationError, match="BIGINT"):
        InvestorFlow(category=Category.INVESTMENT_TRUST, **{field: value})


def test_signed_reported_net_accepts_bigint_endpoints_without_inventing_sides():
    for quantity in (BIGINT_MIN, BIGINT_MAX):
        value = flow(Category.DEALER_TOTAL, net=quantity)
        assert value.resolved_net == quantity
        assert value.buy_quantity is value.sell_quantity is None


def test_inconsistent_reported_net_is_rejected_not_corrected():
    with pytest.raises(InstitutionalValidationError) as error:
        flow(Category.INVESTMENT_TRUST, 10, 4, 7)
    assert error.value.code == "net_mismatch"


def test_missing_side_is_distinct_from_zero_and_is_not_inverse_filled():
    missing = flow(Category.DEALER_HEDGING, sell=0)
    assert missing.resolved_net is None and missing.buy_quantity is None
    explicit_zero = flow(Category.DEALER_HEDGING, 0, 0)
    assert explicit_zero.resolved_net == 0
    reported_only = flow(Category.DEALER_TOTAL, net=-9)
    assert reported_only.resolved_net == -9
    assert reported_only.buy_quantity is reported_only.sell_quantity is None


def test_dealer_and_foreign_subtotals_are_checked_without_double_counting():
    report = synthetic_report()
    assert report.completeness is Completeness.COMPLETE
    assert report.missing_core_categories == ()
    assert report.component(Category.THREE_MAJOR_TOTAL).resolved_net == 550
    assert report.component(Category.DEALER_TOTAL).resolved_net == 50
    assert report.component(Category.FOREIGN_INCLUSIVE).resolved_net == 580
    assert report.component(Category.FOREIGN_DEALERS).aggregation_role is AggregationRole.SUPPLEMENTAL
    assert report.component(Category.FOREIGN_INCLUSIVE).aggregation_role is AggregationRole.SUPPLEMENTAL
    assert report.component(Category.DEALER_TOTAL).aggregation_role is AggregationRole.SUBTOTAL
    assert report.component(Category.DEALER_HEDGING).aggregation_role is AggregationRole.LEAF


@pytest.mark.parametrize("category,incorrect", [
    (Category.DEALER_TOTAL, flow(Category.DEALER_TOTAL, net=51)),
    (Category.THREE_MAJOR_TOTAL, flow(Category.THREE_MAJOR_TOTAL, net=1130)),
    (Category.FOREIGN_INCLUSIVE, flow(Category.FOREIGN_INCLUSIVE, net=600)),
    (Category.DEALER_TOTAL, flow(Category.DEALER_TOTAL, buy=451)),
])
def test_known_verified_subtotal_contradictions_are_rejected(category, incorrect):
    components = tuple(incorrect if item.category is category else item
                       for item in synthetic_components())
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_report(components=components)
    assert error.value.code == "aggregate_mismatch"


def test_tpex_like_incomplete_breakdown_preserves_reported_totals_but_is_partial():
    # SYNTHETIC representation of the incomplete 20-field schema, not live data.
    components = tuple(item for item in synthetic_components()
                       if item.category not in (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING))
    components += (flow(Category.DEALER_HEDGING, sell=250),)
    report = synthetic_report(components=components)
    assert report.completeness is Completeness.PARTIAL
    assert report.missing_core_categories == (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)
    assert report.component(Category.DEALER_TOTAL).resolved_net == 50
    assert report.component(Category.THREE_MAJOR_TOTAL).resolved_net == 550
    assert report.component(Category.DEALER_HEDGING).resolved_net is None
    assert report.component(Category.DEALER_PROPRIETARY) is None


@pytest.mark.parametrize("incorrect_total", [
    flow(Category.THREE_MAJOR_TOTAL, net=551),
    flow(Category.THREE_MAJOR_TOTAL, buy=1651),
    flow(Category.THREE_MAJOR_TOTAL, sell=1101),
])
def test_partial_report_rejects_known_disjoint_top_level_total_contradictions(incorrect_total):
    # SYNTHETIC incomplete TPEx-style report. Its detail is absent, but its
    # independently reported dealer subtotal still makes top-level checks valid.
    components = tuple(item for item in synthetic_components()
        if item.category not in (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING,
                                 Category.THREE_MAJOR_TOTAL)) + (incorrect_total,)
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_report(components=components)
    assert error.value.code == "aggregate_mismatch"


def test_partial_top_level_total_accepts_correct_known_sides_without_filling_detail():
    components = tuple(item for item in synthetic_components()
        if item.category not in (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING,
                                 Category.THREE_MAJOR_TOTAL))
    report = synthetic_report(components=components + (
        flow(Category.THREE_MAJOR_TOTAL, buy=1650, sell=1100, net=550),))
    assert report.completeness is Completeness.PARTIAL
    assert report.component(Category.THREE_MAJOR_TOTAL).resolved_net == 550
    assert report.component(Category.DEALER_PROPRIETARY) is None
    assert report.component(Category.DEALER_HEDGING) is None


def test_top_level_reconciliation_does_not_guess_unknown_subtotal_sides():
    report = synthetic_report(components=(
        flow(Category.FOREIGN_EX_DEALERS, 1000, 400, 600),
        flow(Category.INVESTMENT_TRUST, 200, 300, -100),
        flow(Category.DEALER_TOTAL, net=50),
        flow(Category.THREE_MAJOR_TOTAL, buy=3999, net=550),
    ))
    assert report.completeness is Completeness.PARTIAL
    assert report.component(Category.DEALER_TOTAL).buy_quantity is None
    assert report.component(Category.THREE_MAJOR_TOTAL).sell_quantity is None
    assert report.component(Category.THREE_MAJOR_TOTAL).buy_quantity == 3999


def test_unverified_child_does_not_make_unverified_aggregate_arithmetic_authoritative():
    components = tuple(replace(item, mapping_status=MappingStatus.UNVERIFIED)
                       if item.category is Category.DEALER_PROPRIETARY else
                       flow(Category.DEALER_TOTAL, net=999)
                       if item.category is Category.DEALER_TOTAL else item
                       for item in synthetic_components()
                       if item.category is not Category.THREE_MAJOR_TOTAL)
    report = synthetic_report(components=components)
    assert report.completeness is Completeness.NOT_VERIFIED
    assert report.component(Category.DEALER_TOTAL).resolved_net == 999


def test_legacy_foreign_mapping_cannot_claim_verified_modern_semantics():
    with pytest.raises(InstitutionalValidationError) as error:
        flow(Category.FOREIGN_UNSPECIFIED, 20, 10)
    assert error.value.code == "legacy_mapping_unverified"
    legacy = flow(Category.FOREIGN_UNSPECIFIED, 20, 10,
                  mapping_status=MappingStatus.UNVERIFIED)
    report = synthetic_report(components=(legacy,))
    assert report.completeness is Completeness.NOT_VERIFIED
    assert report.component(Category.FOREIGN_EX_DEALERS) is None
    assert legacy.aggregation_role is AggregationRole.SUPPLEMENTAL


def test_empty_report_is_partial_but_all_explicit_zero_leaves_are_complete():
    missing = synthetic_report(components=())
    assert missing.completeness is Completeness.PARTIAL
    assert len(missing.missing_core_categories) == 4
    zeros = tuple(flow(category, 0, 0) for category in missing.missing_core_categories)
    assert synthetic_report(components=zeros).completeness is Completeness.COMPLETE


@pytest.mark.parametrize("changes", [{"includes_block_trades": None},
    {"includes_odd_lots": None}, {"includes_after_hours": None},
    {"basis": TradeBasis.UNSPECIFIED}, {"code": "unspecified"}])
def test_unknown_scope_prevents_report_completeness(changes):
    scope = synthetic_scope(**changes)
    assert scope.is_verified is False
    assert synthetic_report(scope=scope).completeness is Completeness.NOT_VERIFIED


def test_source_contract_scope_unit_and_taxonomy_are_comparison_dimensions():
    original = synthetic_report()
    alternatives = (
        replace(original, lineage=synthetic_lineage(source_id=UUID(int=999))),
        replace(original, lineage=synthetic_lineage(source_contract_version="synthetic-v2")),
        replace(original, scope=synthetic_scope(includes_block_trades=False)),
        replace(original, scope=synthetic_scope(basis=TradeBasis.CORRECTED)),
        replace(original, quantity_unit=QuantityUnit.UNITS),
        replace(original, taxonomy_version="synthetic-other-taxonomy"),
    )
    assert all(item.comparison_key != original.comparison_key for item in alternatives)


def test_duplicate_categories_are_not_silently_overwritten():
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_report(components=synthetic_components() + (synthetic_components()[0],))
    assert error.value.code == "duplicate_category"


def test_input_lists_are_copied_and_nested_domain_values_are_immutable():
    components = list(synthetic_components())
    report = synthetic_report(components=components)
    components.clear()
    assert len(report.components) == 8 and isinstance(report.components, tuple)
    assert deepcopy(report) == report
    with pytest.raises(FrozenInstanceError):
        report.trade_date = date(2000, 1, 1)
    with pytest.raises(FrozenInstanceError):
        report.components[0].buy_quantity = 999
    dates = [DAY]
    calendar = synthetic_calendar(dates)
    dates.clear()
    reports = [report]
    history = synthetic_history(reports, calendar=calendar)
    reports.clear()
    assert calendar.session_dates == (DAY,) and history.reports == (report,)


def test_statistics_date_and_later_publication_date_remain_distinct():
    report = synthetic_report()
    assert report.trade_date == date(2026, 11, 5)
    assert report.timing.source_published_on == date(2026, 11, 6)
    assert report.timing.source_published_at is None
    assert report.timing.first_seen_at.date() == date(2026, 11, 7)


def test_publication_day_uses_source_timezone_not_utc_day():
    timing = synthetic_timing(source_published_at=datetime(2026, 11, 5, 16, 30, tzinfo=timezone.utc))
    assert timing.source_published_on == date(2026, 11, 6)
    with pytest.raises(InstitutionalValidationError) as error:
        replace(timing, source_published_on=date(2026, 11, 5))
    assert error.value.code == "publication_date_mismatch"


@pytest.mark.parametrize("field", ["first_seen_at", "last_seen_at", "ingested_at", "source_published_at"])
def test_naive_timestamps_are_rejected(field):
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_timing(**{field: datetime(2026, 11, 6, 18)})
    assert error.value.code == "invalid_timestamp"


@pytest.mark.parametrize("changes", [
    {"first_seen_at": SEEN + timedelta(seconds=1)},
    {"ingested_at": SEEN - timedelta(seconds=1)},
])
def test_observation_timestamp_order_is_enforced(changes):
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_timing(**changes)
    assert error.value.code == "invalid_timestamp_order"


def test_missing_publication_metadata_does_not_borrow_observation_time():
    timing = synthetic_timing(source_published_on=None)
    assert timing.source_published_on is timing.source_published_at is None


@pytest.mark.parametrize("value", [SEEN, "2026-11-05"])
def test_trade_dates_cannot_be_datetime_or_source_strings(value):
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_report(trade_date=value)
    assert error.value.code == "invalid_date"


def test_lineage_requires_real_ids_versions_and_fingerprints():
    for changes in ({"source_id": str(SOURCE)}, {"source_contract_version": " "},
                    {"payload_hash": "A" * 64}, {"payload_hash": "not-a-hash"}):
        with pytest.raises(InstitutionalValidationError):
            synthetic_lineage(**changes)


def test_explicit_calendar_can_include_weekend_and_omit_weekday_without_guessing():
    # A Saturday session is intentionally SYNTHETIC, not a real exchange claim.
    saturday = date(2026, 11, 7)
    calendar = synthetic_calendar((DAY, saturday))
    report = synthetic_report(trade_date=saturday)
    history = synthetic_history((report,), calendar=calendar)
    assert history.report_on(saturday) == report
    assert history.report_on(date(2026, 11, 6)) is None
    assert history.calendar.session_dates == (DAY, saturday)


def test_empty_verified_calendar_and_unknown_calendar_are_explicit():
    empty = synthetic_calendar(())
    assert synthetic_history((), calendar=empty).reports == ()
    unknown = synthetic_calendar((DAY,), verified=False)
    history = synthetic_history(calendar=unknown)
    assert history.calendar.verified is False
    assert history.report_on(DAY) is not None


@pytest.mark.parametrize("days", [(DAY, DAY), (DAY + timedelta(days=1), DAY),
    (DAY - timedelta(days=1),), (DAY + timedelta(days=4),), (SEEN,)])
def test_calendar_sessions_must_be_unique_ordered_dates_inside_coverage(days):
    with pytest.raises(InstitutionalValidationError):
        synthetic_calendar(days)


def test_calendar_range_and_verified_flag_are_not_guessed():
    with pytest.raises(InstitutionalValidationError):
        synthetic_calendar((), end=DAY - timedelta(days=1))
    with pytest.raises(InstitutionalValidationError):
        synthetic_calendar(verified=1)


def test_duplicate_dates_are_rejected_even_when_source_revision_differs():
    first = synthetic_report()
    second = replace(first, lineage=synthetic_lineage(source_id=UUID(int=999),
                     revision_id=UUID(int=998)))
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_history((first, second))
    assert error.value.code == "duplicate_or_unsorted_dates"


def test_history_rejects_unsorted_dates_and_mixed_identity():
    later = synthetic_report(trade_date=DAY + timedelta(days=1))
    calendar = synthetic_calendar((DAY, later.trade_date))
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_history((later, synthetic_report()), calendar=calendar)
    assert error.value.code == "duplicate_or_unsorted_dates"
    for changes in ({"instrument_id": UUID(int=999)}, {"market_id": UUID(int=998)}):
        with pytest.raises(InstitutionalValidationError) as error:
            synthetic_history((synthetic_report(**changes),))
        assert error.value.code == "history_identity_mismatch"


def test_history_rejects_another_market_calendar_or_unconfirmed_session():
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_history(calendar=synthetic_calendar(market_id=UUID(int=999)))
    assert error.value.code == "calendar_market_mismatch"
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_history((synthetic_report(trade_date=DAY + timedelta(days=1)),))
    assert error.value.code == "report_outside_sessions"


def test_domain_modules_depend_only_on_standard_library_and_own_domain():
    domain = Path(__file__).resolve().parents[1] / "src/stockapp/modules/institutional/domain"
    for file in domain.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(), filename=str(file))):
            if isinstance(node, ast.Import):
                modules = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert (module.partition(".")[0] in sys.stdlib_module_names
                        or module.startswith("stockapp.modules.institutional.domain")), (file, module)


def test_domain_imports_in_python_without_site_packages():
    source = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "from stockapp.modules.institutional.domain.models import InstitutionalDailyReport; "
        "assert not any(name.split('.')[0] in {'fastapi','sqlalchemy','redis','httpx','psycopg'} "
        "for name in sys.modules); print('domain-only-import-ok')"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "domain-only-import-ok"
