"""SYNTHETIC analytical evidence; never official sessions or broker reports."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.domain.analytics import (
    ALGORITHM_VERSION, AvailabilityPolicy, BrokerAnalysisHistory, BrokerMetricRequest,
    BrokerMetricResult, CalendarSnapshot, CanonicalBrokerInput, MetricStatus,
    PriceObservation, decimal_text, fingerprint, primitive,
)
from stockapp.modules.broker_flow.domain.analytics_context import prepare_context
from stockapp.modules.broker_flow.domain.models import BrokerFlowError, ReportCompleteness
from test_broker_flow_domain import (
    BRANCH, DAY, INSTRUMENT, MARKET, SEEN, SOURCE, synthetic_identity,
    synthetic_lineage, synthetic_report, synthetic_trade,
)


AS_OF = SEEN + timedelta(days=5)


def analytical_request(**changes):
    return BrokerMetricRequest(**(dict(instrument_id=INSTRUMENT, market_id=MARKET,
        metric="broker.branch_net_quantity", start=DAY, end=DAY,
        query_as_of=AS_OF) | changes))


def canonical_input(**changes):
    return CanonicalBrokerInput(**(dict(report=synthetic_report(), observation_id=UUID(int=10001),
        promotion_policy_version="broker-promotion-1.0.0", identities=(synthetic_identity(),),
        canonical_verified=True) | changes))


def calendar(**changes):
    return CalendarSnapshot(**(dict(market_id=MARKET, start=DAY-timedelta(days=10),
        end=DAY+timedelta(days=3), open_dates=(DAY-timedelta(days=1), DAY),
        version="SYNTHETIC-calendar-v1", evidence_reference="SYNTHETIC full local-date coverage",
        verified=True, observed_at=SEEN-timedelta(days=1)) | changes))


def context(**history_changes):
    history = BrokerAnalysisHistory(**(dict(reports=(canonical_input(),)) | history_changes))
    return prepare_context(analytical_request(), history)


def test_one_day_can_validate_canonical_evidence_without_inventing_a_calendar():
    ctx = context()
    assert ctx.status is MetricStatus.COMPLETE and ctx.expected_dates == (DAY,)
    assert ctx.calendar_verified is False and ctx.provisional is True
    assert ctx.point_in_time_supported is False


def test_canonical_selection_requires_an_explicit_assertion():
    pending = canonical_input(canonical_verified=False)
    ctx = context(reports=(pending,))
    assert ctx.status is MetricStatus.NOT_VERIFIED
    result = ctx.result(900, formula="B-S", unit="shares")
    assert result.value is None and result.reasons == ("canonical_selection_unverified",)


@pytest.mark.parametrize("change", [
    {"start": DAY+timedelta(days=1)}, {"end": DAY-timedelta(days=1)},
    {"query_as_of": SEEN.replace(tzinfo=None)}, {"end": AS_OF.date()+timedelta(days=2)},
    {"availability_policy": "observed"}, {"engine_version": "future"},
    {"timezone_name": "Not/A_Timezone"}, {"parameters": {"amount": 1.1}},
])
def test_request_rejects_invalid_windows_versions_times_and_float_metadata(change):
    with pytest.raises(BrokerFlowError):
        analytical_request(**change)


def test_parameters_and_results_are_deeply_immutable():
    settings = {"nested": ["SYNTHETIC"]}
    request = analytical_request(parameters=settings)
    settings["nested"].append("mutated")
    assert request.parameters["nested"] == ("SYNTHETIC",)
    with pytest.raises(TypeError):
        request.parameters["nested"] = ()
    with pytest.raises(FrozenInstanceError):
        request.metric = "changed"
    result = context().result({"rows": [{"quantity": "900", "rank": 1}]}, formula="B-S")
    with pytest.raises(TypeError):
        result.value["rows"][0]["quantity"] = "0"


def test_metadata_depth_size_and_decimal_format_are_bounded():
    nested = {}
    current = nested
    for _ in range(22):
        child = {}
        current["next"] = child
        current = child
    with pytest.raises(BrokerFlowError, match="depth_limit"):
        analytical_request(parameters=nested)
    with pytest.raises(BrokerFlowError, match="size_limit"):
        analytical_request(parameters={"rows": tuple(range(10001))})
    with pytest.raises(BrokerFlowError, match="decimal_size_limit"):
        analytical_request(parameters={"threshold": Decimal("1E+999999999")})
    assert primitive(Decimal("0E+999999999")) == "0"


def test_fingerprint_preserves_exact_inputs_even_when_output_rounds_identically():
    first = analytical_request(parameters={"threshold": Decimal("1.0000000000001")})
    second = analytical_request(parameters={"threshold": Decimal("1.0000000000002")})
    assert fingerprint(first) != fingerprint(second)
    assert primitive(first.parameters)["threshold"] == "1.0000000000001"
    assert decimal_text(first.parameters["threshold"]) == decimal_text(second.parameters["threshold"])
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        assert decimal_text(Decimal("0.1234567890125")) == "0.123456789012"
        assert decimal_text(Decimal("0.1234567890135")) == "0.123456789014"


@pytest.mark.parametrize("invalid", [
    {"open_dates": (DAY, DAY)}, {"open_dates": (DAY, DAY-timedelta(days=1))},
    {"open_dates": (DAY+timedelta(days=20),)}, {"verified": True, "observed_at": SEEN.replace(tzinfo=None)},
])
def test_calendar_requires_exact_ordered_covered_local_dates(invalid):
    with pytest.raises(BrokerFlowError):
        calendar(**invalid)


def test_multiday_cannot_infer_sessions_from_loaded_reports_or_weekdays():
    request = analytical_request(start=DAY-timedelta(days=1))
    ctx = prepare_context(request, BrokerAnalysisHistory(reports=(canonical_input(),)))
    assert ctx.status is MetricStatus.NOT_VERIFIED and ctx.expected_dates == ()
    assert len(ctx.input_refs) == 1
    assert ctx.result(900, formula="sum N").value is None


def test_calendar_not_observed_at_query_cutoff_is_not_verified():
    ctx = prepare_context(analytical_request(start=DAY-timedelta(days=1)),
        BrokerAnalysisHistory(reports=(canonical_input(),), calendar=calendar(observed_at=AS_OF+timedelta(seconds=1))))
    assert ctx.status is MetricStatus.NOT_VERIFIED


@pytest.mark.parametrize("metric,parameters,expected_status", [
    ("broker.branch_net_quantity", {}, MetricStatus.NOT_VERIFIED),
    ("broker.branch_session_cumulative_net", {"sessions": 5}, MetricStatus.INSUFFICIENT_HISTORY),
])
def test_early_window_failures_do_not_expose_future_reports_as_available(
        metric, parameters, expected_status):
    first_seen = AS_OF + timedelta(days=1)
    future = canonical_input(report=synthetic_report(lineage=synthetic_lineage(
        first_seen_at=first_seen, ingested_at=first_seen+timedelta(seconds=1))))
    history = BrokerAnalysisHistory(reports=(future,),
        calendar=None if not parameters else calendar())
    ctx = prepare_context(analytical_request(metric=metric, parameters=parameters,
        start=DAY-timedelta(days=1)), history)
    assert ctx.status is expected_status
    assert ctx.reports == ()
    # Rejected evidence remains explicitly traceable without being an input
    # available for computing financial values or diagnostic alignment rows.
    assert ctx.input_refs[0].first_seen_at == first_seen
    assert ctx.result(900, formula="B-S").value is None


def test_missing_confirmed_session_is_explicit_and_never_zero():
    request = analytical_request(start=DAY-timedelta(days=1))
    ctx = prepare_context(request, BrokerAnalysisHistory(reports=(canonical_input(),), calendar=calendar()))
    assert ctx.status is MetricStatus.MISSING_DATA
    assert ctx.missing_dates == (DAY-timedelta(days=1),)
    assert ctx.result(900, formula="sum N").value is None


def test_report_on_a_confirmed_closed_local_date_is_invalid():
    ctx = context(calendar=calendar(open_dates=(DAY-timedelta(days=1),)))
    assert ctx.status is MetricStatus.INVALID
    assert ctx.reasons == ("report_on_confirmed_closed_date",)
    assert ctx.input_refs[0].revision_id == ctx.history.reports[0].revision_id


@pytest.mark.parametrize("field", ["first_seen_at", "ingested_at", "source_published_at"])
def test_future_observation_ingestion_or_publication_is_not_available(field):
    # Published/first-seen ordering is separately validated by the source model.
    cutoff = SEEN-timedelta(seconds=1) if field != "ingested_at" else SEEN
    report = synthetic_report()
    if field == "source_published_at":
        report = synthetic_report(lineage=synthetic_lineage(source_published_at=SEEN))
    request = analytical_request(query_as_of=cutoff)
    ctx = prepare_context(request, BrokerAnalysisHistory(reports=(canonical_input(report=report),)))
    assert ctx.status is MetricStatus.NOT_YET_AVAILABLE
    assert ctx.result(900, formula="B-S").value is None


def test_unknown_publication_is_provisional_and_strict_policy_blocks():
    visible = context()
    assert visible.result(900, formula="B-S").provisional is True
    request = analytical_request(availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED)
    blocked = prepare_context(request, BrokerAnalysisHistory(reports=(canonical_input(),)))
    assert blocked.status is MetricStatus.NOT_VERIFIED
    assert blocked.reasons == ("source_publication_time_unknown",)


def test_known_publication_alone_does_not_claim_complete_historical_vintages():
    item = canonical_input(report=synthetic_report(lineage=synthetic_lineage(source_published_at=SEEN)))
    result = context(reports=(item,)).result(900, formula="B-S", unit="shares")
    assert result.provisional is False and result.point_in_time_supported is False
    evidenced = context(reports=(item,), point_in_time_verified=True,
        point_in_time_evidence_reference="SYNTHETIC complete as-of canonical selection")
    assert evidenced.result(900, formula="B-S").point_in_time_supported is True


def test_point_in_time_attestation_is_preserved_and_changes_reproducibility_key():
    item = canonical_input(report=synthetic_report(lineage=synthetic_lineage(source_published_at=SEEN)))
    results = tuple(context(reports=(item,), point_in_time_verified=True,
        point_in_time_evidence_reference=evidence).result(900, formula="B-S")
        for evidence in ("SYNTHETIC archived canonical vintages v1",
                         "SYNTHETIC corrected canonical vintages v2"))
    assert all(result.value == 900 and result.point_in_time_supported for result in results)
    assert results[0].details["point_in_time_reference"] == {
        "verified": True, "evidence_reference": "SYNTHETIC archived canonical vintages v1"}
    assert results[0].reproducibility_key != results[1].reproducibility_key
    provisional = context(point_in_time_verified=True,
        point_in_time_evidence_reference="SYNTHETIC asserted vintage archive").result(900, formula="B-S")
    assert provisional.provisional and not provisional.point_in_time_supported
    assert provisional.details["point_in_time_reference"]["verified"] is True
    with pytest.raises(BrokerFlowError, match="point_in_time_evidence"):
        BrokerAnalysisHistory(point_in_time_evidence_reference=123)


@pytest.mark.parametrize("changes,cutoff,status", [
    ({}, SEEN-timedelta(seconds=1), MetricStatus.NOT_YET_AVAILABLE),
    ({"canonical_verified": False}, AS_OF, MetricStatus.NOT_VERIFIED),
    ({"identities": ()}, AS_OF, MetricStatus.NOT_VERIFIED),
])
def test_point_in_time_attestation_does_not_override_rejected_input_evidence(changes, cutoff, status):
    item = canonical_input(report=synthetic_report(lineage=synthetic_lineage(source_published_at=SEEN)),
        **changes)
    ctx = prepare_context(analytical_request(query_as_of=cutoff), BrokerAnalysisHistory(
        reports=(item,), point_in_time_verified=True,
        point_in_time_evidence_reference="SYNTHETIC archive assertion"))
    result = ctx.result(900, formula="B-S")
    assert result.status is status and result.value is None
    assert result.point_in_time_supported is False


@pytest.mark.parametrize("changes", [
    {"branch_id": UUID(int=888)},
    {"branch_code": "SYNTHETIC-other-code"},
])
def test_verified_identity_proof_rejects_overlapping_code_or_branch_assignments(changes):
    first = synthetic_identity()
    conflicting = replace(first, id=UUID(int=889), **changes)
    with pytest.raises(BrokerFlowError, match="overlapping_identity_mapping"):
        canonical_input(identities=(first, conflicting))
    # An unresolved candidate is not an alternative verified assignment.
    item = canonical_input(identities=(first, replace(conflicting, verified=False)))
    assert context(reports=(item,)).status is MetricStatus.COMPLETE


def test_missing_mapping_and_partial_report_do_not_upgrade_completeness():
    assert context(reports=(canonical_input(identities=()),)).status is MetricStatus.NOT_VERIFIED
    report = synthetic_report(completeness=ReportCompleteness.PARTIAL,
        branch_universe_complete=False, trades=(synthetic_trade(buy_quantity=None),))
    assert context(reports=(canonical_input(report=report),)).status is MetricStatus.PARTIAL


def test_omitted_branch_needs_versioned_no_activity_and_a_valid_mapping():
    omitted = UUID(int=777)
    mapping = replace(synthetic_identity(), id=UUID(int=778), branch_id=omitted, branch_code="SYNTHETIC-omitted")
    with pytest.raises(BrokerFlowError, match="no_activity_policy_version"):
        canonical_input(no_activity_branch_ids=frozenset({omitted}),
                        no_activity_evidence_reference="SYNTHETIC omission proof")
    item = canonical_input(identities=(synthetic_identity(), mapping), no_activity_branch_ids=frozenset({omitted}),
        no_activity_evidence_reference="SYNTHETIC omission proof", no_activity_policy_version="SYNTHETIC-omission-v1")
    ctx = context(reports=(item,))
    assert ctx.status is MetricStatus.COMPLETE and ctx.is_no_activity(item, omitted)
    assert ctx.trade(item, omitted) is None
    assert ctx.report_status(replace(item, identities=(synthetic_identity(),))) is MetricStatus.NOT_VERIFIED


def test_duplicate_revision_inputs_are_not_silently_double_counted():
    item = canonical_input()
    with pytest.raises(BrokerFlowError, match="duplicate_report_natural_key"):
        BrokerAnalysisHistory(reports=(item, item))


def test_full_input_lineage_and_version_change_the_reproducibility_key():
    ctx = context()
    result = ctx.result(900, formula="B-S", unit="shares")
    reference = result.input_refs[0]
    assert reference.metadata["observation_id"] == ctx.reports[0].observation_id
    assert reference.metadata["raw_id"] == ctx.reports[0].report.lineage.raw_payload_id
    assert reference.metadata["identities"][0]["identity_version"] == synthetic_identity().identity_version
    output = result.to_dict()
    assert output["value"] == "900" and output["algorithm_version"] == ALGORITHM_VERSION
    assert replace(result, details={"input_revision_changed": True}).reproducibility_key != result.reproducibility_key
    assert len(result.reproducibility_key) == 64


def test_actual_snapshot_values_cannot_share_a_reproducibility_key_by_reusing_ids():
    original = canonical_input()
    changed = replace(original, report=replace(original.report,
        trades=(replace(original.report.trades[0], buy_quantity=1001),)))
    first = context(reports=(original,)).result(900, formula="B-S", unit="shares")
    second = context(reports=(changed,)).result(900, formula="B-S", unit="shares")
    assert first.reproducibility_key != second.reproducibility_key
    assert first.input_refs[0].metadata["snapshot_fingerprint"] != second.input_refs[0].metadata["snapshot_fingerprint"]


def test_full_identity_proof_and_canonical_assertion_remain_traceable_when_rejected():
    results = tuple(context(reports=(canonical_input(identities=(
        synthetic_identity(source_id=source),)),)).result(900, formula="B-S")
        for source in (UUID(int=9001), UUID(int=9002)))
    assert all(result.status is MetricStatus.NOT_VERIFIED and result.value is None for result in results)
    assert results[0].reproducibility_key != results[1].reproducibility_key
    mapping = results[0].input_refs[0].metadata["identities"][0]
    assert (mapping["source_id"], mapping["market_id"], mapping["source_namespace"],
        mapping["broker_code"], mapping["event_kind"], mapping["successor_branch_id"]) == (
            UUID(int=9001), MARKET, "SYNTHETIC-namespace", "000a", "established", None)
    pending = context(reports=(canonical_input(canonical_verified=False),)).result(900, formula="B-S")
    assert pending.input_refs[0].metadata["canonical_verified"] is False


def test_calendar_evidence_and_covered_dates_remain_reproducible():
    first = context(calendar=calendar()).result(900, formula="B-S")
    second = context(calendar=calendar(evidence_reference="SYNTHETIC corrected calendar evidence")).result(900, formula="B-S")
    assert first.reproducibility_key != second.reproducibility_key
    assert first.details["calendar_reference"]["open_dates"] == (DAY,)


def test_count_output_is_integer_and_financial_nested_decimal_rounds_only_at_output():
    count = context().result(5, formula="count", unit="branches")
    assert count.to_dict()["value"] == 5
    ratio = context().result(Decimal("0.2347140039447731"), formula="sum squares", unit="fraction")
    assert ratio.to_dict()["value"] == "0.234714003945"


def test_unsupported_metric_can_have_no_financial_formula_or_algorithm_version():
    result = context().result(None, formula=None, status=MetricStatus.UNSUPPORTED)
    unsupported = replace(result, algorithm_version=None)
    assert unsupported.to_dict()["algorithm_version"] is None
    assert unsupported.formula is None
    with pytest.raises(BrokerFlowError):
        replace(unsupported, status=MetricStatus.COMPLETE)


def test_price_evidence_is_exact_and_duplicate_dates_are_rejected():
    price = PriceObservation(instrument_id=INSTRUMENT, market_id=MARKET, trade_date=DAY,
        close=Decimal("0.10000001"), price_basis="raw", revision_id=UUID(int=22001),
        source_id=SOURCE, source_contract_version="SYNTHETIC-price-v1", first_seen_at=SEEN,
        ingested_at=SEEN+timedelta(seconds=1))
    ctx = context(prices=(price,))
    assert ctx.price_available(price) and ctx.price_reference(price).revision_id == price.revision_id
    with pytest.raises(BrokerFlowError, match="duplicate_price_natural_key"):
        BrokerAnalysisHistory(prices=(price, price))
    with pytest.raises(BrokerFlowError):
        replace(price, close=0.1)
