"""SYNTHETIC broker-flow contracts, never real reports or database seeds."""
import ast
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.domain.identities import BrokerIdentityBook
from stockapp.modules.broker_flow.domain.models import (
    BIGINT_MAX, AuditDisposition, Broker, BrokerBranch, BrokerBranchIdentityHistory,
    BrokerBranchTrade, BrokerFlowError, BrokerIdentityClosure, BrokerIngestionAudit, BrokerMetricDefinition,
    BrokerSourceLineage, BrokerTradeScope, BrokerTradingDailyReport, EntityStatus,
    IdentityEventKind, MetricKind, QuantityUnit, ReportCompleteness,
)


MARKET, SOURCE, RUN, RAW, INSTRUMENT, SCOPE, BROKER, BRANCH, IDENTITY = (
    UUID(int=value) for value in range(101, 110))
DAY = date(2026, 1, 9)
SEEN = datetime(2026, 1, 9, 10, tzinfo=timezone.utc)


def synthetic_identity(**changes):
    return BrokerBranchIdentityHistory(**(dict(id=IDENTITY, branch_id=BRANCH,
        broker_id=BROKER, market_id=MARKET, source_id=SOURCE,
        source_namespace="SYNTHETIC-namespace", broker_code="000a",
        branch_code="000aB", branch_name="SYNTHETIC branch",
        valid_from=date(2020, 1, 1), valid_to=None,
        identity_version="SYNTHETIC-identity-v1",
        evidence_reference="SYNTHETIC-evidence-v1", verified=True) | changes))


def synthetic_scope(**changes):
    return BrokerTradeScope(**(dict(id=SCOPE, source_id=SOURCE, market_id=MARKET,
        code="SYNTHETIC-regular", version="SYNTHETIC-scope-v1",
        session="SYNTHETIC-regular-only", participant_universe="SYNTHETIC-excludes-pure-dealers",
        correction_basis="SYNTHETIC-reported", description="SYNTHETIC scope only",
        odd_lot_included=False, block_trade_included=False, verified=True) | changes))


def test_identity_requires_explicit_verification_before_lookup():
    verified = synthetic_identity()
    fields = {name: getattr(verified, name) for name in verified.__dataclass_fields__
              if name != "verified"}
    pending = BrokerBranchIdentityHistory(**fields)
    assert pending.verified is False
    assert BrokerIdentityBook(identities=(pending,)).resolve(
        source_id=SOURCE, market_id=MARKET, source_namespace=pending.source_namespace,
        branch_code=pending.branch_code, trade_date=DAY) is None


def synthetic_lineage(**changes):
    return BrokerSourceLineage(**(dict(source_id=SOURCE, run_id=RUN, raw_payload_id=RAW,
        source_namespace="SYNTHETIC-namespace", source_contract_version="SYNTHETIC-contract-v1",
        schema_version="SYNTHETIC-schema-v1", parser_version="SYNTHETIC-parser-v1",
        identity_policy_version="as_reported_branch_v1", payload_sha256="a" * 64,
        first_seen_at=SEEN, ingested_at=SEEN + timedelta(seconds=1)) | changes))


def synthetic_trade(**changes):
    return BrokerBranchTrade(**(dict(branch_id=BRANCH, identity_id=IDENTITY,
        buy_quantity=1000, sell_quantity=100, quantity_unit=QuantityUnit.SHARES,
        row_locators=("SYNTHETIC/main.csv/row=2", "SYNTHETIC/main.csv/row=3")) | changes))


def synthetic_report(**changes):
    return BrokerTradingDailyReport(**(dict(instrument_id=INSTRUMENT, market_id=MARKET,
        trade_date=DAY, scope=synthetic_scope(), lineage=synthetic_lineage(),
        trades=(synthetic_trade(),), quantity_unit=QuantityUnit.SHARES,
        completeness=ReportCompleteness.COMPLETE, branch_universe_complete=True,
        price_basis="SYNTHETIC-raw", price_quantity_basis="SYNTHETIC-per-share",
        date_evidence_reference="SYNTHETIC-source-header-date") | changes))


@pytest.mark.parametrize("buy,sell,expected", [(1000, 100, 900), (200, 800, -600),
    (600, 500, 100), (0, 300, -300), (100, 0, 100), (0, 200, -200),
    (50, 50, 0), (0, 0, 0), (BIGINT_MAX, 0, BIGINT_MAX),
    (0, BIGINT_MAX, -BIGINT_MAX)])
def test_synthetic_net_quantities_are_exact(buy, sell, expected):
    assert synthetic_trade(buy_quantity=buy, sell_quantity=sell,
                           reported_net_quantity=expected).net_quantity == expected


@pytest.mark.parametrize("buy,sell", [(None, 0), (0, None), (None, None)])
def test_missing_is_not_zero_and_partial_keeps_null(buy, sell):
    trade = synthetic_trade(buy_quantity=buy, sell_quantity=sell)
    assert trade.net_quantity is None
    report = synthetic_report(trades=(trade,), completeness=ReportCompleteness.PARTIAL,
                              branch_universe_complete=False)
    assert report.trades[0].buy_quantity is buy
    assert report.trades[0].sell_quantity is sell


def test_report_does_not_force_an_unproven_global_balance_identity():
    report = synthetic_report()
    assert report.trades[0].buy_quantity != report.trades[0].sell_quantity
    assert report.completeness is ReportCompleteness.COMPLETE


@pytest.mark.parametrize("field", ["buy_quantity", "sell_quantity"])
@pytest.mark.parametrize("invalid", [-1, BIGINT_MAX + 1, 1.0, Decimal(1), True, "1"])
def test_quantity_rejects_negative_bool_float_and_overflow(field, invalid):
    with pytest.raises(BrokerFlowError, match="invalid_" + field):
        synthetic_trade(**{field: invalid})


@pytest.mark.parametrize("invalid", [True, 1.0, Decimal(1), "900", BIGINT_MAX + 1])
def test_reported_net_is_a_bounded_integer(invalid):
    with pytest.raises(BrokerFlowError, match="invalid_reported_net_quantity"):
        synthetic_trade(reported_net_quantity=invalid)


def test_source_reported_net_must_match_both_known_sides():
    with pytest.raises(BrokerFlowError, match="net_quantity_mismatch"):
        synthetic_trade(reported_net_quantity=901)
    # A reported net alone does not manufacture missing buy or sell quantities.
    trade = synthetic_trade(buy_quantity=None, reported_net_quantity=900)
    assert trade.reported_net_quantity == 900
    assert trade.net_quantity is None


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("0.10000001"),
    Decimal("0.200000020000"), Decimal("1.0000000000000"),
    Decimal("99999999999999999999999999.999999999999")])
def test_exact_amounts_preserve_precision_under_hostile_decimal_context(amount):
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        trade = synthetic_trade(buy_amount=amount, sell_amount=Decimal("0"))
        result = synthetic_report(trades=(trade,), currency="TWD")
    assert result.trades[0].buy_amount == amount


@pytest.mark.parametrize("amount", [1.0, True, "1", Decimal("NaN"),
    Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("-1"),
    Decimal("0.0000000000001"), Decimal("100000000000000000000000000")])
def test_amounts_reject_float_nan_negative_overflow_and_nonzero_overprecision(amount):
    with pytest.raises(BrokerFlowError):
        synthetic_trade(buy_amount=amount)


def test_amounts_need_exact_currency_and_a_known_price_quantity_basis():
    trade = synthetic_trade(buy_amount=Decimal("12.50"))
    with pytest.raises(BrokerFlowError, match="unverified_amount_basis"):
        synthetic_report(trades=(trade,))
    with pytest.raises(BrokerFlowError, match="unverified_amount_basis"):
        synthetic_report(trades=(trade,), currency="TWD", price_quantity_basis="unknown")


@pytest.mark.parametrize("invalid", ["twd", "TW", "TWD ", "", 1, True])
def test_currency_is_explicit_and_never_guessed(invalid):
    with pytest.raises(BrokerFlowError, match="invalid_currency"):
        synthetic_report(currency=invalid)


@pytest.mark.parametrize("scope", [synthetic_scope(source_id=UUID(int=901)),
    synthetic_scope(market_id=UUID(int=902))])
def test_reports_reject_market_or_source_scope_mixing(scope):
    with pytest.raises(BrokerFlowError, match="report_scope_mismatch"):
        synthetic_report(scope=scope)


def test_different_sources_and_scopes_have_distinct_canonical_keys():
    original = synthetic_report()
    different_scope = synthetic_report(scope=synthetic_scope(id=UUID(int=903),
                                                            code="SYNTHETIC-block"))
    different_source = synthetic_report(scope=synthetic_scope(id=UUID(int=904),
        source_id=UUID(int=905)), lineage=synthetic_lineage(source_id=UUID(int=905)))
    assert len({original.natural_key, different_scope.natural_key, different_source.natural_key}) == 3


@pytest.mark.parametrize("field,value", [("source_contract_version", "SYNTHETIC-v2"),
    ("schema_version", "SYNTHETIC-schema-v2"), ("parser_version", "SYNTHETIC-parser-v2"),
    ("identity_policy_version", "SYNTHETIC-policy-v2"),
    ("source_namespace", "SYNTHETIC-other")])
def test_profile_changes_are_incompatible_even_for_the_same_natural_key(field, value):
    first = synthetic_report()
    other = synthetic_report(lineage=replace(first.lineage, **{field: value}))
    assert first.natural_key == other.natural_key
    assert first.comparison_key != other.comparison_key


def test_report_does_not_accept_duplicate_branches_or_reused_source_rows():
    first = synthetic_trade()
    with pytest.raises(BrokerFlowError, match="duplicate_branch_trade"):
        synthetic_report(trades=(first, first))
    second = replace(first, branch_id=UUID(int=999), identity_id=UUID(int=998))
    with pytest.raises(BrokerFlowError, match="row_locator_used_by_multiple_branches"):
        synthetic_report(trades=(first, second))


def test_aggregated_price_bucket_provenance_retains_all_raw_row_locators():
    trade = synthetic_trade(row_locators=["SYNTHETIC/row=2", "SYNTHETIC/row=3"])
    assert trade.row_locators == ("SYNTHETIC/row=2", "SYNTHETIC/row=3")
    with pytest.raises(BrokerFlowError, match="duplicate_row_locator"):
        replace(trade, row_locators=("SYNTHETIC/row=2", "SYNTHETIC/row=2"))


@pytest.mark.parametrize("changes,reason", [
    ({"trades": (synthetic_trade(buy_quantity=None),)}, "complete_report_has_missing_sides"),
    ({"branch_universe_complete": False}, "unverified_complete_report"),
    ({"scope": synthetic_scope(verified=False)}, "unverified_complete_report"),
    ({"trades": (synthetic_trade(quantity_unit=QuantityUnit.UNITS),)}, "incompatible_quantity_units"),
])
def test_complete_report_is_not_a_label_for_an_unverified_subset(changes, reason):
    with pytest.raises(BrokerFlowError, match=reason):
        synthetic_report(**changes)


@pytest.mark.parametrize("invalid", ["shares", "lots", "kg", 1000, None])
def test_canonical_units_require_a_typed_base_unit(invalid):
    with pytest.raises(BrokerFlowError, match="invalid_quantity_unit"):
        synthetic_trade(quantity_unit=invalid)


def test_unknown_scope_capabilities_are_not_false_or_an_automatic_union():
    scope = synthetic_scope(odd_lot_included=None, block_trade_included=None)
    assert scope.odd_lot_included is None
    assert scope.block_trade_included is None
    assert scope.comparison_key != synthetic_scope().comparison_key


def test_null_publication_is_not_observation_or_generation_time():
    lineage = synthetic_lineage(source_generated_at=SEEN - timedelta(minutes=1))
    assert lineage.source_published_at is None
    assert lineage.source_generated_at < lineage.first_seen_at < lineage.ingested_at


@pytest.mark.parametrize("field,value,reason", [
    ("first_seen_at", SEEN.replace(tzinfo=None), "invalid_first_seen_at"),
    ("ingested_at", SEEN - timedelta(seconds=1), "ingested_before_first_seen"),
    ("source_generated_at", SEEN + timedelta(seconds=1), "source_generated_at_after_first_seen"),
    ("source_published_at", SEEN + timedelta(seconds=1), "source_published_at_after_first_seen"),
    ("source_published_at", SEEN.replace(tzinfo=None), "invalid_source_published_at"),
])
def test_timing_invariants_have_separate_roles(field, value, reason):
    with pytest.raises(BrokerFlowError, match=reason):
        synthetic_lineage(**{field: value})


def test_generated_cannot_be_later_than_publication():
    with pytest.raises(BrokerFlowError, match="generated_after_published"):
        synthetic_lineage(source_generated_at=SEEN,
                           source_published_at=SEEN - timedelta(seconds=1))


def test_market_local_date_is_not_replaced_by_utc_calendar_date():
    seen = datetime(2026, 1, 8, 16, 1, tzinfo=timezone.utc)
    report = synthetic_report(lineage=synthetic_lineage(first_seen_at=seen,
        ingested_at=seen), trade_date=date(2026, 1, 9))
    assert report.trade_date != seen.date()
    with pytest.raises(BrokerFlowError, match="future_source_date"):
        replace(report, trade_date=date(2026, 1, 10))


@pytest.mark.parametrize("value", ["not-a-zone", "", True])
def test_market_timezone_must_be_explicit_and_valid(value):
    with pytest.raises(BrokerFlowError):
        synthetic_report(market_timezone=value)


def test_code_identity_keeps_case_and_leading_zeroes():
    identity = synthetic_identity()
    book = BrokerIdentityBook(identities=(identity,))
    assert identity.branch_code == "000aB"
    query = dict(source_id=SOURCE, market_id=MARKET, source_namespace="SYNTHETIC-namespace",
                 trade_date=DAY)
    assert book.resolve(**query, branch_code="000aB") == identity
    for code in ("000AB", "aB", "000a", "SYNTHETIC branch"):
        assert book.resolve(**query, branch_code=code) is None


def test_identity_half_open_boundary_supports_verified_rename():
    boundary = date(2026, 1, 1)
    old = synthetic_identity(valid_to=boundary)
    new = synthetic_identity(id=UUID(int=910), valid_from=boundary,
        branch_name="SYNTHETIC renamed branch", identity_version="SYNTHETIC-v2",
        event_kind=IdentityEventKind.RENAME)
    book = BrokerIdentityBook(identities=(old, new))
    assert old.effective_on(boundary - timedelta(days=1))
    assert not old.effective_on(boundary)
    assert new.effective_on(boundary)
    assert old.branch_id == new.branch_id
    assert book.resolve(source_id=SOURCE, market_id=MARKET,
        source_namespace="SYNTHETIC-namespace", branch_code="000aB", trade_date=boundary) == new


def test_code_change_has_explicit_evidence_and_no_prefix_matching():
    boundary = date(2026, 1, 1)
    old = synthetic_identity(valid_to=boundary)
    new = synthetic_identity(id=UUID(int=911), valid_from=boundary,
        branch_code="00aBC", event_kind=IdentityEventKind.CODE_CHANGE)
    book = BrokerIdentityBook(identities=(old, new))
    query = dict(source_id=SOURCE, market_id=MARKET,
                 source_namespace="SYNTHETIC-namespace", trade_date=DAY)
    assert book.resolve(**query, branch_code="000aB") is None
    assert book.resolve(**query, branch_code="00aBC").branch_id == BRANCH


def test_code_reuse_and_merger_do_not_roll_old_facts_into_the_successor():
    successor = UUID(int=912)
    boundary = date(2026, 1, 1)
    old = synthetic_identity(valid_to=boundary, event_kind=IdentityEventKind.MERGER,
                             successor_branch_id=successor)
    new = synthetic_identity(id=UUID(int=913), branch_id=successor,
        valid_from=boundary, event_kind=IdentityEventKind.CODE_REUSE)
    book = BrokerIdentityBook(identities=(old, new))
    query = dict(source_id=SOURCE, market_id=MARKET,
                 source_namespace="SYNTHETIC-namespace", branch_code="000aB")
    assert book.resolve(**query, trade_date=boundary - timedelta(days=1)).branch_id == BRANCH
    assert book.resolve(**query, trade_date=DAY).branch_id == successor
    # An unchanged old report continues to reference the predecessor UUID.
    earlier = synthetic_report(trade_date=boundary - timedelta(days=1))
    book.validate_report(earlier)
    assert earlier.trades[0].branch_id == BRANCH


@pytest.mark.parametrize("event_kind", [IdentityEventKind.RENAME, IdentityEventKind.CODE_CHANGE,
    IdentityEventKind.MERGER, IdentityEventKind.CLOSURE])
def test_later_closure_retains_original_identity_and_separate_recording_time(event_kind):
    original = synthetic_identity()
    closure = BrokerIdentityClosure(identity_id=original.id, valid_to=DAY,
        evidence_reference="SYNTHETIC-future-closure-evidence", closed_at=SEEN,
        event_kind=event_kind)
    assert closure.valid_to == DAY
    assert closure.closed_at == SEEN
    assert original.valid_to is None
    assert original.event_kind is IdentityEventKind.ESTABLISHED
    assert original.branch_id == BRANCH
    with pytest.raises(FrozenInstanceError):
        closure.valid_to = date(2020, 1, 1)


def test_merger_closure_is_an_edge_and_does_not_roll_up_identity_book():
    original = synthetic_identity()
    closure = BrokerIdentityClosure(identity_id=original.id, valid_to=DAY,
        evidence_reference="SYNTHETIC-merger-evidence", closed_at=SEEN,
        event_kind=IdentityEventKind.MERGER, successor_branch_id=UUID(int=928))
    assert closure.successor_branch_id != BRANCH
    book = BrokerIdentityBook(identities=(original,))
    assert book.resolve(source_id=SOURCE, market_id=MARKET,
        source_namespace="SYNTHETIC-namespace", branch_code="000aB",
        trade_date=DAY).branch_id == BRANCH


@pytest.mark.parametrize("changes", [
    {"event_kind": IdentityEventKind.ESTABLISHED},
    {"event_kind": IdentityEventKind.CODE_REUSE},
    {"event_kind": "closure"},
    {"event_kind": IdentityEventKind.RENAME, "successor_branch_id": UUID(int=929)},
    {"closed_at": SEEN.replace(tzinfo=None)}, {"evidence_reference": ""},
    {"valid_to": datetime(2026, 1, 9, tzinfo=timezone.utc)},
])
def test_closure_rejects_invalid_event_successor_timing_and_missing_evidence(changes):
    arguments = dict(identity_id=IDENTITY, valid_to=DAY,
        evidence_reference="SYNTHETIC-evidence", closed_at=SEEN)
    with pytest.raises(BrokerFlowError):
        BrokerIdentityClosure(**(arguments | changes))


@pytest.mark.parametrize("changes", [
    {"valid_to": date(2020, 1, 1)}, {"valid_to": date(2019, 12, 31)},
    {"valid_from": datetime(2020, 1, 1, tzinfo=timezone.utc)},
    {"branch_code": 1234}, {"branch_code": " 000aB"}, {"broker_code": ""},
    {"evidence_reference": ""}, {"successor_branch_id": BRANCH,
        "event_kind": IdentityEventKind.MERGER},
    {"successor_branch_id": UUID(int=914)},
])
def test_invalid_or_unproven_historical_identity_is_rejected(changes):
    with pytest.raises(BrokerFlowError):
        synthetic_identity(**changes)


def test_identity_book_rejects_overlapping_mappings_across_versions_or_branch_ids():
    first = synthetic_identity()
    second = synthetic_identity(id=UUID(int=915), branch_id=UUID(int=916),
        valid_from=date(2026, 1, 1), identity_version="SYNTHETIC-v2")
    with pytest.raises(BrokerFlowError, match="overlapping_identity_mapping"):
        BrokerIdentityBook(identities=(first, second))


def test_same_branch_cannot_have_overlapping_verified_code_assignments():
    original = synthetic_identity()
    other_code = synthetic_identity(id=UUID(int=925), branch_code="000abC")
    with pytest.raises(BrokerFlowError, match="overlapping_identity_mapping"):
        BrokerIdentityBook(identities=(original, other_code))


def test_unverified_candidates_do_not_ambiguate_verified_mapping():
    original = synthetic_identity()
    candidate = synthetic_identity(id=UUID(int=926), branch_id=UUID(int=927), verified=False)
    book = BrokerIdentityBook(identities=(original, candidate))
    query = dict(source_id=SOURCE, market_id=MARKET,
        source_namespace="SYNTHETIC-namespace", branch_code="000aB", trade_date=DAY)
    assert book.resolve(**query) == original
    assert book.resolve(**query, identity_version="SYNTHETIC-identity-v1") == original
    assert book.resolve(**query, identity_version="SYNTHETIC-absent") is None


@pytest.mark.parametrize("changes", [{"code": "x" * 101}, {"version": "x" * 101}])
def test_scope_identifiers_fit_persistence_limits(changes):
    with pytest.raises(BrokerFlowError):
        synthetic_scope(**changes)


@pytest.mark.parametrize("changes", [{"algorithm_id": "x" * 129}, {"version": "x" * 65}])
def test_metric_identifiers_fit_persistence_limits(changes):
    arguments = dict(algorithm_id="broker.branch_net_quantity", version="1.0.0",
        name="SYNTHETIC metadata", kind=MetricKind.DERIVED,
        formula="buy_quantity - sell_quantity", output_unit="shares")
    with pytest.raises(BrokerFlowError):
        BrokerMetricDefinition(**(arguments | changes))


def test_same_code_in_other_market_source_or_namespace_is_a_distinct_mapping():
    original = synthetic_identity()
    identities = (original,
        synthetic_identity(id=UUID(int=917), market_id=UUID(int=918)),
        synthetic_identity(id=UUID(int=919), source_id=UUID(int=920)),
        synthetic_identity(id=UUID(int=921), source_namespace="SYNTHETIC-elsewhere"))
    assert len(BrokerIdentityBook(identities=identities).identities) == 4


@pytest.mark.parametrize("changes,reason", [
    ({"verified": False}, "unresolved_branch_identity"),
    ({"branch_id": UUID(int=922)}, "incompatible_branch_identity"),
    ({"source_id": UUID(int=923)}, "incompatible_branch_identity"),
    ({"market_id": UUID(int=924)}, "incompatible_branch_identity"),
    ({"source_namespace": "SYNTHETIC-elsewhere"}, "incompatible_branch_identity"),
    ({"valid_to": DAY}, "incompatible_branch_identity"),
])
def test_report_identity_validation_never_misassigns_unknown_or_outdated_branches(changes, reason):
    book = BrokerIdentityBook(identities=(synthetic_identity(**changes),))
    with pytest.raises(BrokerFlowError, match=reason):
        book.validate_report(synthetic_report())


def test_unknown_mapping_cannot_canonicalize_even_a_partial_report():
    book = BrokerIdentityBook(identities=())
    with pytest.raises(BrokerFlowError, match="unresolved_branch_identity"):
        book.validate_report(synthetic_report(completeness=ReportCompleteness.PARTIAL,
                                              branch_universe_complete=False))


def test_models_are_deeply_immutable_at_all_collection_boundaries():
    rows = ["SYNTHETIC/row=2"]
    trade = synthetic_trade(row_locators=rows)
    trades = [trade]
    report = synthetic_report(trades=trades)
    rows.append("SYNTHETIC/row=3")
    trades.clear()
    assert report.trades == (trade,)
    assert report.trades[0].row_locators == ("SYNTHETIC/row=2",)
    with pytest.raises(FrozenInstanceError):
        report.lineage.source_namespace = "altered"
    with pytest.raises(FrozenInstanceError):
        report.trades[0].buy_quantity = 0


def test_broker_and_branch_keep_stable_uuid_and_explicit_lifecycle_status():
    broker = Broker(id=BROKER, name="SYNTHETIC broker", status=EntityStatus.MERGED)
    branch = BrokerBranch(id=BRANCH, broker_id=broker.id, status=EntityStatus.CLOSED)
    assert branch.broker_id == BROKER
    assert branch.status is EntityStatus.CLOSED
    with pytest.raises(BrokerFlowError, match="invalid_branch_status"):
        replace(branch, status="active")


def test_metric_definition_is_explanatory_versioned_metadata():
    parameters = [["top_n", "15"]]
    definition = BrokerMetricDefinition(algorithm_id="proxy.broker_top_n_net_imbalance",
        version="1.0.0", name="SYNTHETIC metadata", kind=MetricKind.PROXY,
        formula="sum(top_positive_net) + sum(top_negative_net)",
        output_unit="shares", parameters=parameters)
    parameters[0][1] = "30"
    assert definition.parameters == (("top_n", "15"),)
    assert not hasattr(definition, "calculate")
    unsupported = BrokerMetricDefinition(algorithm_id="investor.medium_large_net_flow",
        version=None, name="Unavailable investor identification", kind=MetricKind.UNSUPPORTED,
        formula=None, output_unit="shares")
    assert unsupported.formula is None
    with pytest.raises(BrokerFlowError, match="unsupported_metric_has_algorithm"):
        replace(unsupported, version="1.0.0")


def test_metric_parameters_reject_float_and_duplicate_names():
    arguments = dict(algorithm_id="broker.top_branches", version="1.0.0",
        name="SYNTHETIC top branches", kind=MetricKind.DERIVED,
        formula="period first, then sort", output_unit="shares")
    with pytest.raises(BrokerFlowError):
        BrokerMetricDefinition(**arguments, parameters=(("top_n", 15.0),))
    with pytest.raises(BrokerFlowError, match="duplicate_metric_parameter"):
        BrokerMetricDefinition(**arguments, parameters=(("top_n", "5"), ("top_n", "15")))


def test_unresolved_item_audit_preserves_the_opaque_source_code():
    audit = BrokerIngestionAudit(run_id=RUN, raw_payload_id=RAW, source_id=SOURCE,
        row_locator="SYNTHETIC/main.csv/row=19", disposition=AuditDisposition.UNRESOLVED,
        reason="unknown_branch_identity", observed_at=SEEN,
        source_branch_code="000aB", instrument_id=INSTRUMENT, trade_date=DAY)
    assert audit.branch_id is None and audit.identity_id is None
    assert audit.source_branch_code == "000aB"
    with pytest.raises(BrokerFlowError, match="unresolved_audit_has_identity"):
        replace(audit, branch_id=BRANCH, identity_id=IDENTITY)


def test_domain_imports_are_independent_of_frameworks_and_io():
    domain = Path(__file__).resolve().parents[1] / "src/stockapp/modules/broker_flow/domain"
    forbidden = {"fastapi", "sqlalchemy", "redis", "httpx", "requests", "urllib3",
                 "psycopg", "finmind", "finlab", "tejapi"}
    for path in domain.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            assert not {name.split(".")[0].lower() for name in names} & forbidden, path
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(domain.parents[3])
    code = ("import importlib, sys; "
            "[importlib.import_module('stockapp.modules.broker_flow.domain.' + name) "
            "for name in ('models', 'identities', 'ports', 'persistence')]; "
            "assert not set(sys.modules) & " + repr(forbidden))
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
