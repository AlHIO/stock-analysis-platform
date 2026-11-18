"""SYNTHETIC broker HTTP queries; writes use disposable PostgreSQL schemas only.

The configured production database is never seeded, migrated or modified by
this file. Run it through scripts/run_institutional_validation.py so the test
fixture's private schemas also live in a dedicated temporary database.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import os
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_broker_flow_postgres import (
    AT, DAY, SyntheticReferences, archive, report, save,
)
from stockapp.modules.broker_flow.domain.models import (
    Broker, BrokerBranch, BrokerBranchIdentityHistory, BrokerTradeScope,
)
from stockapp.modules.broker_flow.infrastructure.repositories import (
    CANONICAL, REVISIONS, TRADES, ITEMS, SqlAlchemyBrokerFlowRepository,
    SqlAlchemyBrokerIdentityRepository,
)
from stockapp.modules.market_data.domain.models import DataSource, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="Explicit disposable PostgreSQL required")]


@pytest.fixture
def refs(database):
    """Supported market code, explicitly synthetic instruments and source only."""
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(code="TWSE", name="SYNTHETIC broker API market",
            timezone="Asia/Taipei", currency="TWD"))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id,
            symbol="SYNTH_BROKER_HTTP", name="SYNTHETIC broker API security"))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(code="SYNTHETIC_BROKER_QUERY",
            name="SYNTHETIC lawful test source", source_type=SourceType.FILE))
        registry = SqlAlchemyBrokerIdentityRepository(session)
        broker = Broker(id=uuid4(), name="SYNTHETIC broker")
        registry.add_broker(broker)
        branches = tuple(BrokerBranch(id=UUID(int=index+1), broker_id=broker.id) for index in range(2))
        identities = tuple(BrokerBranchIdentityHistory(id=uuid4(), branch_id=branch.id, broker_id=broker.id,
            market_id=market.id, source_id=source.id, source_namespace="SYNTHETIC", broker_code="0000",
            branch_code=code, branch_name="SYNTHETIC " + code, valid_from=date(2010, 1, 1), valid_to=None,
            identity_version="SYNTHETIC-map-v1", evidence_reference="SYNTHETIC identity evidence", verified=True)
            for branch, code in zip(branches, ("00Aa", "00aa")))
        for branch, identity in zip(branches, identities):
            registry.add_branch(branch)
            registry.add_identity(identity)
        scope = BrokerTradeScope(id=uuid4(), source_id=source.id, market_id=market.id,
            code="SYNTHETIC-regular", version="SYNTHETIC-scope-v1", session="SYNTHETIC-regular",
            participant_universe="SYNTHETIC observed eligible branches", correction_basis="SYNTHETIC-final",
            description="SYNTHETIC scope excludes other counterparties", verified=True,
            odd_lot_included=False, block_trade_included=False)
    return SyntheticReferences(market, instrument, source, broker, branches, identities, scope)


def synthetic_day(database, refs, *, day=DAY, buy=1000, at=AT, partial=False,
                  scope=None, amounts=False, row_index=0, publication=None):
    """Archive and promote one explicit SYNTHETIC source snapshot."""
    raw = archive(database, refs, at=at, tag="SYNTHETIC-query-" + day.isoformat())
    value = replace(report(refs, raw, buy=buy, partial=partial, publication=publication), trade_date=day)
    if scope is not None:
        value = replace(value, scope=scope)
    if amounts:
        value = replace(value, trades=(
            replace(value.trades[0], buy_amount=Decimal("100.100000000001"), sell_amount=Decimal("0")),
            value.trades[1],
        ))
    written = save(database, value, row_index=row_index)
    return value, raw, written


def client(database, *, calendar_reader=None):
    from stockapp.modules.broker_flow.api.routes import router, install_error_handlers
    from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
    app = FastAPI()
    app.state.market_query_engine = database
    app.include_router(router)
    common_errors(app)
    install_error_handlers(app)
    if calendar_reader is not None:
        from stockapp.modules.broker_flow.api.dependencies import get_broker_query_service
        from stockapp.modules.broker_flow.application.query_service import BrokerQueryService
        from stockapp.modules.broker_flow.infrastructure.query_repository import SqlAlchemyBrokerQueryRepository

        def query():
            with Session(database) as session, session.begin():
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                yield BrokerQueryService(SqlAlchemyBrokerQueryRepository(session, calendar_reader=calendar_reader))

        app.dependency_overrides[get_broker_query_service] = query
    return TestClient(app)


def path(refs, suffix=""):
    return f"/api/v1/stocks/{refs.instrument.id}/broker-flow" + suffix


def test_routes_exist_and_missing_instrument_is_not_empty_success(database, refs):
    with client(database) as http:
        assert http.get(path(refs)).status_code == 200
        assert http.get(path(refs, "/latest")).status_code == 200
        assert http.get(path(refs, "/top-branches")).status_code == 200
        assert http.get(path(refs, "/metrics")).status_code == 200
        assert http.get("/api/v1/broker-flow/status").status_code == 200
        for suffix in ("", "/latest", "/top-branches", "/metrics"):
            response = http.get(f"/api/v1/stocks/{uuid4()}/broker-flow" + suffix)
            assert response.status_code == 404, response.text
            assert "postgresql://" not in response.text and "SELECT " not in response.text


@pytest.mark.parametrize("suffix,params", [
    ("", {"limit": 0}), ("", {"limit": 251}),
    ("", {"from": "2026-11-13", "to": "2026-11-12"}),
    ("", {"from": "2026-11-01T00:00:00Z"}),
    ("", {"timeframe": "1w"}), ("", {"order": "random"}),
    ("/top-branches", {"top_n": 7}),
    ("/top-branches", {"side": "gross_buyers"}),
    ("/metrics", {"algorithm": "unexplained.main_force_score"}),
    ("/metrics", {"algorithm_parameters": '{"threshold_amount":1.1}'}),
    ("/metrics", {"algorithm_parameters": "{"}),
])
def test_invalid_http_parameters_are_rejected_in_real_database_context(database, refs, suffix, params):
    with client(database) as http:
        response = http.get(path(refs, suffix), params=params)
    assert response.status_code == 422, response.text
    assert "postgresql://" not in response.text and "SELECT " not in response.text


def test_existing_empty_security_has_no_synthetic_reports_or_zero_metrics(database, refs):
    with client(database) as http:
        assert http.get(path(refs)).json()["items"] == []
        assert http.get(path(refs, "/latest")).json()["point"] is None
        response = http.get(path(refs, "/metrics"), params={
            "from": str(DAY), "to": str(DAY), "algorithm": "broker.branch_net_quantity"})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["value"] is None
        assert not result["point_in_time_supported"]


def test_exact_quantities_amounts_identity_lineage_and_unknown_publication(database, refs):
    value, raw, written = synthetic_day(database, refs, amounts=True)
    with client(database) as http:
        response = http.get(path(refs, "/latest"))
    assert response.status_code == 200, response.text
    point = response.json()["point"]
    first = point["trades"][0]
    assert first["buy_quantity"] == "1000" and first["sell_quantity"] == "100"
    assert first["net_quantity"] == "900"
    assert first["buy_amount"] == "100.100000000001" and first["net_amount"] == "100.100000000001"
    assert first["identity"]["branch_code"] == "00Aa"
    assert first["identity"]["branch_name"] == "SYNTHETIC 00Aa"
    assert first["identity"]["verified"] is True
    assert point["quantity_unit"] == "shares" and point["completeness"] == "complete"
    assert point["branch_universe_complete"] is True
    assert point["lineage"]["revision_id"] == str(written.revision_id)
    assert point["lineage"]["source"]["id"] == str(refs.source.id)
    assert point["lineage"]["raw_payload_id"] == str(raw.id)
    assert point["lineage"]["run_id"] == str(raw.run_id)
    assert point["lineage"]["payload_sha256"] == raw.sha256
    assert point["timing"]["trade_date"] == str(DAY)
    assert point["timing"]["source_published_at"] is None
    assert point["timing"]["source_generated_at"] is None
    assert point["promotion_policy_version"] == "broker-promotion-1.0.0"


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_same_date_different_scope_keyset_pages_and_latest_ambiguity(database, refs, order):
    second_scope = replace(refs.scope, id=uuid4(), code="SYNTHETIC-odd-lot",
                           session="SYNTHETIC-odd-lot", odd_lot_included=True)
    synthetic_day(database, refs)
    synthetic_day(database, refs, scope=second_scope, at=AT+timedelta(minutes=1))
    with client(database) as http:
        params = {"from": str(DAY), "to": str(DAY), "limit": 1, "order": order}
        first = http.get(path(refs), params=params)
        assert first.status_code == 200, first.text
        page = first.json()
        assert page["count"] == 1 and page["next_cursor"]
        second = http.get(path(refs), params=params | {"cursor": page["next_cursor"]})
        assert second.status_code == 200, second.text
        last = second.json()
        assert last["count"] == 1 and last["next_cursor"] is None
        scopes = [page["items"][0]["scope"]["id"], last["items"][0]["scope"]["id"]]
        assert len(set(scopes)) == 2
        assert scopes == sorted(scopes, reverse=order == "desc")
        assert http.get(path(refs, "/latest")).status_code == 422
        chosen = http.get(path(refs, "/latest"), params={"trade_scope": str(second_scope.id)})
        assert chosen.status_code == 200 and chosen.json()["point"]["scope"]["id"] == str(second_scope.id)
        changed_filter = http.get(path(refs), params=params | {
            "cursor": page["next_cursor"], "trade_scope": str(second_scope.id)})
        assert changed_filter.status_code == 422


def test_branch_and_broker_filters_preserve_full_report_count_and_ranking_universe(database, refs):
    synthetic_day(database, refs)
    with client(database) as http:
        filtered = http.get(path(refs), params={"branch": str(refs.branches[0].id)}).json()["items"][0]
        assert filtered["filtered"] is True and filtered["total_branch_count"] == 2
        assert len(filtered["trades"]) == 1 and filtered["trades"][0]["net_quantity"] == "900"
        broker = http.get(path(refs), params={"broker": str(refs.broker.id)}).json()["items"][0]
        assert len(broker["trades"]) == 2
        base = {"from": str(DAY), "to": str(DAY), "top_n": 5, "side": "net_buyers"}
        ranked = http.get(path(refs, "/top-branches"), params=base)
        assert ranked.status_code == 200, ranked.text
        row = ranked.json()["result"]["value"][0]
        assert row["rank"] == 1 and row["branch_id"] == str(refs.branches[0].id)
        assert row["net_quantity"] == "900"
        excluded = http.get(path(refs, "/top-branches"), params=base | {"branch": str(refs.branches[1].id)})
        assert excluded.status_code == 200, excluded.text
        assert excluded.json()["result"]["value"] == []


def test_same_day_different_sources_are_separate_and_require_explicit_aggregation_selection(database, refs):
    with Session(database) as session, session.begin():
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(code="SYNTHETIC_BROKER_QUERY_SECOND",
            name="SYNTHETIC independent source", source_type=SourceType.FILE))
        identities = tuple(replace(identity, id=uuid4(), source_id=source.id) for identity in refs.identities)
        registry = SqlAlchemyBrokerIdentityRepository(session)
        for identity in identities:
            registry.add_identity(identity)
    second = replace(refs, source=source, identities=identities,
        scope=replace(refs.scope, id=uuid4(), source_id=source.id))
    synthetic_day(database, refs)
    synthetic_day(database, second, buy=1500)
    with client(database) as http:
        payload = http.get(path(refs), params={"from": str(DAY), "to": str(DAY)}).json()
        assert payload["count"] == 2
        observed = {point["lineage"]["source"]["id"]: point["trades"][0]["net_quantity"]
                    for point in payload["items"]}
        assert observed == {str(refs.source.id): "900", str(source.id): "1400"}
        assert http.get(path(refs, "/top-branches"), params={"from": str(DAY), "to": str(DAY)}).status_code == 422
        selected = http.get(path(refs, "/top-branches"), params={
            "from": str(DAY), "to": str(DAY), "source": str(source.id)})
        assert selected.status_code == 200, selected.text
        assert selected.json()["result"]["value"][0]["net_quantity"] == "1400"
        assert selected.json()["sources"][0]["id"] == str(source.id)


def test_duplicate_revision_stale_and_partial_candidates_do_not_rewind_api(database, refs):
    original, _, first = synthetic_day(database, refs)
    _, _, repeated = synthetic_day(database, refs, at=AT+timedelta(minutes=1))
    assert repeated.revision_id == first.revision_id
    _, _, newer = synthetic_day(database, refs, buy=1500, at=AT+timedelta(minutes=3))
    _, _, stale = synthetic_day(database, refs, buy=1250, at=AT+timedelta(minutes=2))
    _, _, partial = synthetic_day(database, refs, partial=True, at=AT+timedelta(minutes=4))
    assert stale.decision.value == "stale" and partial.decision.value == "candidate"
    with client(database) as http:
        response = http.get(path(refs, "/latest"))
    assert response.status_code == 200, response.text
    point = response.json()["point"]
    assert point["lineage"]["revision_id"] == str(newer.revision_id)
    assert point["trades"][0]["net_quantity"] == "1400"
    assert point["completeness"] == "complete"
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(CANONICAL)) == 1
        assert session.scalar(select(func.count()).select_from(REVISIONS)) == 4


def test_rolled_back_snapshot_never_appears_in_http(database, refs):
    raw = archive(database, refs)
    value = report(refs, raw)
    with pytest.raises(RuntimeError, match="SYNTHETIC rollback"):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerFlowRepository(session).upsert(value, row_index=0, raw_row={"SYNTHETIC": True})
            raise RuntimeError("SYNTHETIC rollback")
    with client(database) as http:
        assert http.get(path(refs)).json()["items"] == []
        assert http.get(path(refs, "/latest")).json()["point"] is None
    with Session(database) as session:
        for table in (CANONICAL, REVISIONS, TRADES, ITEMS):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_default_unverified_calendar_does_not_invent_complete_interval_statistics(database, refs):
    synthetic_day(database, refs, day=DAY-timedelta(days=1))
    synthetic_day(database, refs)
    with client(database) as http:
        response = http.get(path(refs, "/top-branches"), params={
            "from": str(DAY-timedelta(days=1)), "to": str(DAY), "top_n": 5})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["status"] == "not_verified" and result["value"] is None
    assert "verified_calendar_coverage_required" in result["reasons"]
    assert not result["point_in_time_supported"]


class SyntheticCalendar:
    """Explicit synthetic full coverage; never infer real exchange holidays."""
    def __init__(self, dates):
        self.dates = tuple(sorted(dates))

    def load(self, market_id, start, end, query_as_of):
        from stockapp.modules.broker_flow.public import CalendarSnapshot
        return CalendarSnapshot(market_id=market_id, start=start, end=end,
            open_dates=tuple(day for day in self.dates if start <= day <= end),
            version="SYNTHETIC-H5-calendar-v1", evidence_reference="SYNTHETIC known calendar",
            verified=True, observed_at=AT-timedelta(days=20))


def test_verified_session_cumulative_streak_and_formula_use_domain_engine(database, refs):
    dates = (date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), DAY)
    for index, day in enumerate(dates):
        synthetic_day(database, refs, day=day, buy=index*100)
    with client(database, calendar_reader=SyntheticCalendar(dates)) as http:
        query = {"from": str(dates[0]), "to": str(DAY), "branch": str(refs.branches[0].id),
                 "algorithm": "broker.branch_session_cumulative_net", "algorithm_parameters": '{"sessions":5}'}
        cumulative = http.get(path(refs, "/metrics"), params=query)
        assert cumulative.status_code == 200, cumulative.text
        result = cumulative.json()["result"]
        assert result["status"] == "complete" and result["value"] == "500"
        assert result["expected_dates"] == [str(day) for day in dates]
        assert result["algorithm_version"] == "1.0.0" and result["formula"]
        assert len(result["input_refs"]) == 5 and result["provisional"] is True
        assert result["point_in_time_supported"] is False
        streak = http.get(path(refs, "/metrics"), params=query | {
            "algorithm": "broker.branch_buy_streak", "algorithm_parameters": "{}"})
        assert streak.status_code == 200, streak.text
        assert streak.json()["result"]["status"] == "complete"
        assert streak.json()["result"]["value"] == 3


def test_missing_branch_in_complete_daily_delivery_is_not_silently_no_activity(database, refs):
    dates = (DAY-timedelta(days=1), DAY)
    raw = archive(database, refs, tag="SYNTHETIC absent branch")
    previous = replace(report(refs, raw), trade_date=dates[0])
    save(database, replace(previous, trades=previous.trades[1:]))
    synthetic_day(database, refs)
    with client(database, calendar_reader=SyntheticCalendar(dates)) as http:
        response = http.get(path(refs, "/metrics"), params={
            "from": str(dates[0]), "to": str(DAY), "branch": str(refs.branches[0].id),
            "algorithm": "broker.branch_range_net_quantity"})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["status"] == "missing_data" and result["value"] is None
    assert result["missing_dates"] == [str(dates[0])]


def test_request_snapshot_keeps_canonical_revision_consistent_during_writer_commit(database, refs):
    _, _, initial = synthetic_day(database, refs)
    changed = []

    def write_after_snapshot_started(conn, cursor, statement, parameters, context, executemany):
        if not changed and "from instruments" in statement.lower():
            # Mark before nested SQL so the independent writer never recurses.
            changed.append(True)
            _, _, revision = synthetic_day(database, refs, buy=1500, at=AT+timedelta(minutes=1))
            changed.append(revision.revision_id)

    event.listen(database, "after_cursor_execute", write_after_snapshot_started)
    try:
        with client(database) as http:
            response = http.get(path(refs, "/latest"))
            assert response.status_code == 200, response.text
            point = response.json()["point"]
            assert point["lineage"]["revision_id"] == str(initial.revision_id)
            assert point["trades"][0]["net_quantity"] == "900"
    finally:
        event.remove(database, "after_cursor_execute", write_after_snapshot_started)
    assert len(changed) == 2, "SYNTHETIC writer must actually commit during the HTTP transaction"
    with client(database) as http:
        response = http.get(path(refs, "/latest"))
        assert response.status_code == 200, response.text
        assert response.json()["point"]["lineage"]["revision_id"] == str(changed[1])
        assert response.json()["point"]["trades"][0]["net_quantity"] == "1400"


def test_http_reads_are_bounded_read_only_and_never_fetch_provider_or_raw(database, refs, monkeypatch):
    from stockapp.modules.broker_flow.infrastructure.providers import LicensedBrokerAdapter, OfflineBrokerAdapter
    synthetic_day(database, refs, day=DAY-timedelta(days=1))
    synthetic_day(database, refs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Broker GET attempted provider download or Raw parsing")

    monkeypatch.setattr(LicensedBrokerAdapter, "fetch", forbidden)
    monkeypatch.setattr(OfflineBrokerAdapter, "parse", forbidden)
    statements = []

    def track(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(database, "before_cursor_execute", track)
    try:
        with client(database) as http:
            response = http.get(path(refs), params={"from": "2026-11-01", "to": str(DAY), "limit": 1})
            assert response.status_code == 200, response.text
            assert response.json()["count"] == 1
            assert http.get(path(refs, "/latest")).status_code == 200
            assert http.get(path(refs, "/top-branches"), params={"from": str(DAY), "to": str(DAY)}).status_code == 200
            assert http.get(path(refs, "/metrics"), params={"from": str(DAY), "to": str(DAY)}).status_code == 200
            assert http.get("/api/v1/broker-flow/status").status_code == 200
    finally:
        event.remove(database, "before_cursor_execute", track)
    assert any("repeatable read read only" in statement for statement in statements)
    assert not any(statement.lstrip().startswith(("insert ", "update ", "delete ", "alter ")) for statement in statements)
    assert not any("body" in statement or "raw_row" in statement or "config_json" in statement for statement in statements)
    pages = [statement for statement in statements
             if "broker_flow_daily_reports" in statement and "order by" in statement
             and "broker_flow_revisions.branch_count" in statement]
    assert pages and all("limit" in statement for statement in pages)


def test_trade_budget_is_checked_before_large_snapshot_json_is_read(database, refs, monkeypatch):
    from stockapp.modules.broker_flow.infrastructure import query_repository
    synthetic_day(database, refs)
    monkeypatch.setattr(query_repository, "MAX_QUERY_TRADES", 1)
    statements = []

    def track(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(database, "before_cursor_execute", track)
    try:
        with client(database) as http:
            response = http.get(path(refs))
    finally:
        event.remove(database, "before_cursor_execute", track)
    assert response.status_code == 422, response.text
    assert "broker_query_limit_exceeded" in response.text
    assert any("branch_count" in statement for statement in statements)
    assert not any("report_json" in statement for statement in statements)


def test_status_preserves_counts_and_source_dates_without_secret_error_or_config(database, refs):
    from stockapp.modules.market_data.domain.models import IngestionStatus
    from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyIngestionRunRepository
    _, raw, _ = synthetic_day(database, refs)
    with Session(database) as session, session.begin():
        runs = SqlAlchemyIngestionRunRepository(session)
        run = runs.get(raw.run_id)
        runs.update(run.finish(status=IngestionStatus.FAILED, finished_at=AT+timedelta(minutes=5),
            stats={"accepted": 2, "api_key": 1},
            error_summary="SYNTHETIC failure postgresql://private-user:private-secret@example.invalid/private-db"))
    with client(database) as http:
        response = http.get("/api/v1/broker-flow/status", params={"source": str(refs.source.id)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "private-user" not in response.text and "private-secret" not in response.text
    assert "postgresql://" not in response.text and "api_key" not in response.text
    source = next(item for item in payload["items"] if item["source"]["id"] == str(refs.source.id))
    assert source["canonical_count"] == 1 and source["revision_count"] == 1
    assert source["latest_canonical_date"] == str(DAY)
    assert source["latest_run"]["status"] == "failed"
    assert source["latest_run"]["counts"] == {"accepted": 2}
    assert source["latest_run"]["safe_failure_code"] == "ingestion_failed"
    assert source["capabilities"]["automated_ingestion_enabled"] is False
