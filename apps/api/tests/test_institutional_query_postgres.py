"""SYNTHETIC Institutional REST/Repository integration in private schemas only.

The parent test launcher supplies a dedicated PostgreSQL database. No provider
requests, production securities, or inferred real trading calendars are used.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.main import create_app
from stockapp.modules.institutional.domain.calculations import InstitutionalCalculator
from stockapp.modules.institutional.domain.models import (
    CalendarSnapshot, Category, FactLineage, InstitutionalDailyReport,
    InstitutionalHistory, InvestorFlow, QuantityUnit, SourceTiming, TradeBasis,
    TradeScope,
)
from stockapp.modules.institutional.domain.persistence import (
    PromotionPolicy, RawPayload, SourceAudit,
)
from stockapp.modules.institutional.infrastructure.models import (
    InstitutionalIngestionItemRecord, InstitutionalReportRecord,
    InstitutionalReportRevisionRecord,
)
from stockapp.modules.institutional.infrastructure.repositories import (
    SqlAlchemyInstitutionalAuditRepository, SqlAlchemyInstitutionalRepository,
)
from stockapp.modules.market_data.domain.models import (
    DataSource, IngestionRun, Instrument, InstrumentType, Market, SessionStatus,
    SourceType, TradingCalendar,
)
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
    SqlAlchemyTradingCalendarRepository,
)

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Dedicated PostgreSQL test target required")]

START = date(2026, 1, 1)
SESSIONS = tuple(START + timedelta(days=index) for index in range(60))
END = SESSIONS[-1]
SEEN = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
SCOPE = TradeScope(code="synthetic_verified_all", includes_block_trades=True,
    includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL)


@dataclass(frozen=True)
class Dataset:
    market: Market
    source: DataSource
    instrument: Instrument
    empty: Instrument
    unsupported: Instrument


@pytest.fixture
def records(database, request):
    market_code = getattr(request, "param", "TWSE")
    source_code = "TWSE_INSTITUTIONAL_T86" if market_code == "TWSE" else "TPEX_INSTITUTIONAL_OPENAPI"
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(
            code=market_code, name="Synthetic institutional API market", timezone="Asia/Taipei", currency="TWD"))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code=source_code, name="Synthetic institutional API source", source_type=SourceType.MANUAL))
        instruments = SqlAlchemyInstrumentRepository(session)
        instrument = instruments.add(Instrument(market_id=market.id, symbol="SYNTH1", name="Synthetic API stock"))
        empty = instruments.add(Instrument(market_id=market.id, symbol="SYNTH2", name="Synthetic empty stock"))
        unsupported = instruments.add(Instrument(market_id=market.id, symbol="SYNTH_ETF",
            name="Synthetic unsupported ETF", instrument_type=InstrumentType.ETF, quantity_unit="units"))
    return Dataset(market, source, instrument, empty, unsupported)


def archive(database, records, *, at=SEEN, source=None):
    """One synthetic raw may contain many independently identified daily rows."""
    source = source or records.source
    run = IngestionRun(source_id=source.id, started_at=at)
    raw = RawPayload(run_id=run.id, source_id=source.id, market_id=records.market.id,
        endpoint="https://example.invalid/synthetic/institutional-query", body=b"SYNTHETIC fixed institutional observations",
        content_type="text/plain", status_code=200, fetched_at=at)
    with Session(database) as session, session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        audits = SqlAlchemyInstitutionalAuditRepository(session)
        audits.add_source_audit(SourceAudit(run_id=run.id, source_id=source.id, market_id=records.market.id))
        audits.save_raw(raw)
    return raw


def observation(records, raw, day, *, net=1, scope=SCOPE, contract="synthetic-query-v1",
                taxonomy="tw-institutional-v1", published=None, components=None, row_index=0):
    if components is None:
        components = (
            InvestorFlow(category=Category.FOREIGN_EX_DEALERS,
                buy_quantity=max(net, 0), sell_quantity=max(-net, 0), net_quantity=net),
            InvestorFlow(category=Category.INVESTMENT_TRUST, buy_quantity=20, sell_quantity=0, net_quantity=20),
            InvestorFlow(category=Category.DEALER_PROPRIETARY, buy_quantity=3, sell_quantity=0, net_quantity=3),
            InvestorFlow(category=Category.DEALER_HEDGING, buy_quantity=0, sell_quantity=4, net_quantity=-4),
        )
    return InstitutionalDailyReport(instrument_id=records.instrument.id, market_id=records.market.id,
        trade_date=day, quantity_unit=QuantityUnit.SHARES, scope=scope, components=components,
        taxonomy_version=taxonomy,
        lineage=FactLineage(source_id=raw.source_id, source_contract_version=contract,
            parser_version="synthetic-query-parser-v1", revision_id=uuid4(), run_id=raw.run_id,
            raw_id=raw.id, source_row_locator=f"synthetic/daily/{row_index}", payload_hash="0" * 64),
        timing=SourceTiming(first_seen_at=raw.fetched_at, last_seen_at=raw.fetched_at,
            ingested_at=raw.fetched_at + timedelta(seconds=1), source_published_at=published))


def save(database, report, *, row_index=0, primary_source=None):
    policy = PromotionPolicy(source_id=primary_source or report.lineage.source_id,
        source_contract_version=report.lineage.source_contract_version, quantity_unit=report.quantity_unit,
        taxonomy_version=report.taxonomy_version)
    with Session(database) as session, session.begin():
        return SqlAlchemyInstitutionalRepository(session, policy).upsert(report, row_index=row_index)


def series(database, records, *, missing=(), values=None):
    raw = archive(database, records)
    reports = []
    with Session(database) as session, session.begin():
        repository = SqlAlchemyInstitutionalRepository(session, PromotionPolicy(
            source_id=records.source.id, source_contract_version="synthetic-query-v1", quantity_unit=QuantityUnit.SHARES))
        for index, day in enumerate(SESSIONS):
            if day in missing:
                continue
            report = observation(records, raw, day, net=values[index] if values else index + 1, row_index=index)
            repository.upsert(report, row_index=index)
            reports.append(report)
    return tuple(reports)


class SyntheticConfirmedCalendar:
    """Explicit test-only sessions, including weekends; never a weekday guess."""
    def __init__(self, sessions=SESSIONS):
        self.sessions = sessions

    def load(self, market_id, *, start, end):
        return CalendarSnapshot(market_id=market_id, start=start, end=end,
            session_dates=tuple(day for day in self.sessions if start <= day <= end),
            verified=True, version="synthetic-confirmed-sessions-v1")


def query_service(session, *, confirmed=False):
    from stockapp.modules.institutional.application.query_service import InstitutionalQueryService
    from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    return InstitutionalQueryService(SqlAlchemyInstitutionalQueryRepository(session,
        calendar_reader=SyntheticConfirmedCalendar() if confirmed else None), now=lambda: NOW)


def client_for(database, *, confirmed=False):
    from stockapp.modules.institutional.api.dependencies import get_institutional_service
    app = create_app()
    def dependency():
        with Session(database.execution_options(isolation_level="REPEATABLE READ")) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield query_service(session, confirmed=confirmed)
    app.dependency_overrides[get_institutional_service] = dependency
    return TestClient(app)


def path(records, suffix=""):
    return f"/api/v1/stocks/{records.instrument.id}/institutional{suffix}"


def parameters(**extra):
    return {"from": START.isoformat(), "to": END.isoformat()} | extra


def component(point, category):
    return next(item for item in point["components"] if item["category"] == category)


def cumulative(body, period):
    return next(item for item in body["cumulative"] if item["period"] == period)


def test_http_history_latest_and_source_revision_metadata(database, records):
    raw = archive(database, records)
    published = SEEN - timedelta(hours=3)
    original = observation(records, raw, START, net=9007199254741009, published=published)
    persisted = save(database, original)
    with client_for(database) as client:
        response = client.get(path(records), params=parameters())
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1 and body["next_cursor"] is None
        point = body["items"][0]
        foreign = component(point, "foreign_ex_dealers")
        assert foreign["buy_quantity"] == foreign["net_quantity"] == "9007199254741009"
        assert foreign["sell_quantity"] == "0"
        assert point["provenance"]["revision_id"] == str(persisted.revision_id)
        assert point["provenance"]["source"]["id"] == str(records.source.id)
        assert point["provenance"]["source"]["name"] == records.source.name
        assert point["provenance"]["raw_id"] == str(raw.id)
        assert point["provenance"]["run_id"] == str(raw.run_id)
        assert point["provenance"]["source_contract_version"] == "synthetic-query-v1"
        assert point["timing"]["source_published_at"].replace("Z", "+00:00") == published.isoformat()
        assert point["timing"]["first_seen_at"].replace("Z", "+00:00") == SEEN.isoformat()
        assert point["timing"]["as_of_date"] == START.isoformat()
        assert point["trade_scope"]["includes_block_trades"] is True
        latest = client.get(path(records, "/latest"))
        assert latest.status_code == 200 and latest.json()["point"] == point
        assert body["freshness"]["is_realtime"] is False
        assert body["coverage"]["completeness"] == "not_verified"


def test_source_null_and_known_zero_remain_distinct(database, records):
    raw = archive(database, records)
    null_flow = (InvestorFlow(category=Category.FOREIGN_EX_DEALERS, sell_quantity=0),)
    zero_flow = (InvestorFlow(category=Category.FOREIGN_EX_DEALERS, buy_quantity=0, sell_quantity=0, net_quantity=0),)
    save(database, observation(records, raw, SESSIONS[0], components=null_flow), row_index=0)
    save(database, observation(records, raw, SESSIONS[1], components=zero_flow, row_index=1), row_index=1)
    with client_for(database) as client:
        points = client.get(path(records), params=parameters()).json()["items"]
    assert component(points[0], "foreign_ex_dealers")["net_quantity"] is None
    assert component(points[0], "foreign_ex_dealers")["sell_quantity"] == "0"
    assert component(points[1], "foreign_ex_dealers")["net_quantity"] == "0"


@pytest.mark.parametrize("records", ["TWSE", "TPEX"], indirect=True)
def test_partial_canonical_does_not_backfill_missing_dealer_leaves(database, records):
    raw = archive(database, records)
    partial = observation(records, raw, START, components=(
        InvestorFlow(category=Category.FOREIGN_EX_DEALERS, buy_quantity=100, sell_quantity=0, net_quantity=100),
        InvestorFlow(category=Category.INVESTMENT_TRUST, buy_quantity=20, sell_quantity=0, net_quantity=20),
        InvestorFlow(category=Category.DEALER_TOTAL, net_quantity=10),
        InvestorFlow(category=Category.THREE_MAJOR_TOTAL, net_quantity=130),
    ))
    save(database, partial)
    with client_for(database) as client:
        response = client.get(path(records, "/latest"))
        assert response.status_code == 200
        point = response.json()["point"]
        assert point["quality"]["status"] == "partial"
        assert component(point, "dealer_total")["net_quantity"] == "10"
        assert component(point, "dealer_proprietary")["net_quantity"] is None
        assert component(point, "dealer_hedging")["net_quantity"] is None
        derived = {item["category"]: item for item in point["derived_daily"]}
        assert derived["three_major_total"]["value"] is None
        assert derived["dealer_total"]["value"] is None
        assert derived["foreign_ex_dealers"]["value"] == "100"


def test_candidate_observations_are_never_promoted_by_query(database, records):
    raw = archive(database, records)
    published = SEEN - timedelta(hours=1)
    report = observation(records, raw, START, scope=replace(SCOPE, includes_block_trades=None), published=published)
    save(database, report)
    with client_for(database) as client:
        history = client.get(path(records), params=parameters())
        latest = client.get(path(records, "/latest"))
        assert history.status_code == latest.status_code == 200
        assert history.json()["items"] == [] and latest.json()["point"] is None
        # A 2B-2 observation without a 2B-4 job still has dated audit metadata.
        status = client.get("/api/v1/chip-data/status", params={"market": records.market.code}).json()["items"][0]
        assert status["candidate_count"] == 1 and status["canonical_count"] == 0
        assert status["latest_reported_as_of_date"] == START.isoformat()
        assert status["latest_canonical_date"] is None
        assert status["latest_source_published_at"].replace("Z", "+00:00") == published.isoformat()
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InstitutionalReportRecord)) == 0
        assert session.scalar(select(func.count()).select_from(InstitutionalReportRevisionRecord)) == 1


def test_empty_existing_stock_is_200_and_unsupported_or_unknown_is_404(database, records):
    with client_for(database) as client:
        prefix = f"/api/v1/stocks/{records.empty.id}/institutional"
        assert client.get(prefix, params=parameters()).json()["items"] == []
        assert client.get(prefix + "/latest").json()["point"] is None
        for identifier in (uuid4(), records.unsupported.id):
            assert client.get(f"/api/v1/stocks/{identifier}/institutional").status_code == 404


@pytest.mark.parametrize("extra", [
    {"from": "2026-02-01", "to": "2026-01-01"},
    {"from": "2000-01-01", "to": "2026-01-01"},
    {"to": "2026-12-01"}, {"limit": 1001}, {"limit": 0},
    {"from": "2026-02-30"}, {"order": "random"}, {"cursor": "not-a-cursor"},
])
def test_invalid_ranges_and_cursor_are_http_422(database, records, extra):
    with client_for(database) as client:
        response = client.get(path(records), params=parameters(**extra))
    assert response.status_code == 422


def test_keyset_pagination_and_query_signature(database, records):
    series(database, records)
    with client_for(database) as client:
        first = client.get(path(records), params=parameters(limit=2)).json()
        assert [point["trade_date"] for point in first["items"]] == [day.isoformat() for day in SESSIONS[:2]]
        assert first["next_cursor"]
        second = client.get(path(records), params=parameters(limit=2, cursor=first["next_cursor"])).json()
        assert [point["trade_date"] for point in second["items"]] == [day.isoformat() for day in SESSIONS[2:4]]
        mismatched = client.get(path(records), params=parameters(limit=2, cursor=first["next_cursor"], order="desc"))
        assert mismatched.status_code == 422


def test_unknown_scope_is_empty_but_same_code_different_flags_is_ambiguous(database, records):
    raw = archive(database, records)
    save(database, observation(records, raw, START))
    other = replace(SCOPE, includes_block_trades=False)
    save(database, observation(records, raw, START, scope=other, row_index=1), row_index=1)
    with client_for(database) as client:
        assert client.get(path(records), params=parameters(trade_scope="nonexistent_scope")).json()["items"] == []
        assert client.get(path(records), params=parameters()).status_code == 422
        assert client.get(path(records), params=parameters(trade_scope=SCOPE.code)).status_code == 422
        selected = client.get(path(records), params=parameters(trade_scope=json.dumps({
            "code": SCOPE.code, "includes_block_trades": False, "includes_odd_lots": True,
            "includes_after_hours": True, "basis": "original"})))
        assert selected.status_code == 200 and selected.json()["count"] == 1
        assert selected.json()["items"][0]["trade_scope"]["includes_block_trades"] is False


def test_all_windows_match_existing_decimal_calculator_with_confirmed_sessions(database, records):
    reports = series(database, records)
    calendar = SyntheticConfirmedCalendar().load(records.market.id, start=START, end=END)
    history = InstitutionalHistory(instrument_id=records.instrument.id, market_id=records.market.id,
        calendar=calendar, reports=reports)
    expected = InstitutionalCalculator().cumulative_windows(history, category=Category.FOREIGN_EX_DEALERS, as_of=END)
    with client_for(database, confirmed=True) as client:
        response = client.get(path(records, "/metrics"), params=parameters())
        assert response.status_code == 200
        body = response.json()
        assert body["calendar"]["verified"] is True
        for result in expected:
            actual = cumulative(body, result.period)
            assert actual["value"] == str(result.value)
            assert actual["status"] == result.status.value
        assert [cumulative(body, period)["value"] for period in (5, 10, 20, 60)] == ["290", "555", "1010", "1830"]


def test_production_calendar_is_unverified_even_when_reports_are_dense(database, records):
    series(database, records)
    with Session(database) as session, session.begin():
        calendars = SqlAlchemyTradingCalendarRepository(session)
        for day in SESSIONS:
            calendars.upsert(TradingCalendar(market_id=records.market.id, trade_date=day, session_status=SessionStatus.OPEN))
    with client_for(database) as client:
        response = client.get(path(records, "/metrics"), params=parameters())
        assert response.status_code == 200
        body = response.json()
        assert body["calendar"]["verified"] is False
        assert all(item["value"] is None and item["status"] == "not_verified" for item in body["cumulative"])
        assert body["streak"]["status"] == "not_verified"


def test_missing_confirmed_session_is_not_skipped_in_windows_or_streak(database, records):
    missing = SESSIONS[-3]
    series(database, records, missing=(missing,))
    with client_for(database, confirmed=True) as client:
        body = client.get(path(records, "/metrics"), params=parameters()).json()
    assert cumulative(body, 5)["value"] is None and cumulative(body, 5)["status"] == "missing_data"
    assert missing.isoformat() in cumulative(body, 5)["missing_dates"]
    assert body["streak"]["status"] == "missing_data"
    assert body["streak"]["buy_count"] is None
    assert body["streak"]["buy_lower_bound"] == 2


@pytest.mark.parametrize("last_values,buy,sell", [([1, 2, 3, 4, 5], 5, 0), ([-1, -2, -3, -4, -5], 0, 5), ([0, 0, 0, 0, 0], 0, 0)])
def test_confirmed_buy_sell_and_zero_streaks(last_values, buy, sell, database, records):
    boundary = -1 if buy else 1
    values = [0] * 54 + [boundary] + last_values
    series(database, records, values=values)
    with client_for(database, confirmed=True) as client:
        body = client.get(path(records, "/metrics"), params=parameters()).json()
    assert body["streak"]["status"] == "complete"
    assert body["streak"]["buy_count"] == buy and body["streak"]["sell_count"] == sell


def test_derived_sum_exceeding_bigint_remains_exact_decimal_string(database, records):
    amount = 2**63 - 1
    series(database, records, values=[amount] * 60)
    with client_for(database, confirmed=True) as client:
        response = client.get(path(records, "/metrics"), params=parameters(period=60))
        assert response.status_code == 200
        value = cumulative(response.json(), 60)["value"]
    assert value == str(Decimal(amount) * 60)
    assert int(value) > 2**63 - 1


@pytest.mark.parametrize("profile_axis", ["source", "contract", "taxonomy"])
def test_incompatible_profiles_are_visible_but_never_combined(database, records, profile_axis):
    """Different daily canonical profiles can exist without becoming one series."""
    ordinary_raw = archive(database, records)
    alternate_source = records.source
    if profile_axis == "source":
        with Session(database) as session, session.begin():
            alternate_source = SqlAlchemyDataSourceRepository(session).add(DataSource(
                code="SYNTHETIC_OTHER_PROFILE", name="Synthetic alternate provider", source_type=SourceType.MANUAL))
    alternate_raw = archive(database, records, source=alternate_source)
    with Session(database) as session, session.begin():
        for index, day in enumerate(SESSIONS):
            changed = index >= 55
            report = observation(records, alternate_raw if changed else ordinary_raw, day, net=index + 1,
                contract="synthetic-query-v2" if changed and profile_axis == "contract" else "synthetic-query-v1",
                taxonomy="synthetic-taxonomy-v2" if changed and profile_axis == "taxonomy" else "tw-institutional-v1",
                row_index=index)
            repository = SqlAlchemyInstitutionalRepository(session, PromotionPolicy(
                source_id=report.lineage.source_id, source_contract_version=report.lineage.source_contract_version,
                taxonomy_version=report.taxonomy_version, quantity_unit=report.quantity_unit))
            repository.upsert(report, row_index=index)
    with client_for(database, confirmed=True) as client:
        response = client.get(path(records, "/metrics"), params=parameters())
        assert response.status_code == 200
        body = response.json()
        assert cumulative(body, 5)["value"] == "290" and cumulative(body, 5)["status"] == "complete"
        assert cumulative(body, 60)["value"] is None
        assert cumulative(body, 60)["status"] == "incompatible_data"
        assert body["streak"]["status"] == "incompatible_data"
        assert body["streak"]["buy_count"] is None


def test_missing_source_quantity_propagates_through_confirmed_metrics(database, records):
    series(database, records)
    raw = archive(database, records, at=SEEN + timedelta(minutes=5))
    save(database, observation(records, raw, END, components=(
        InvestorFlow(category=Category.FOREIGN_EX_DEALERS, sell_quantity=0),)))
    with client_for(database, confirmed=True) as client:
        response = client.get(path(records, "/metrics"), params=parameters())
        assert response.status_code == 200
        body = response.json()
        assert body["daily"]["value"] is None
        assert cumulative(body, 5)["value"] is None and cumulative(body, 5)["status"] == "missing_data"
        assert body["streak"]["buy_count"] is None and body["streak"]["sell_count"] is None


def test_revision_lineage_refresh_and_stale_observation_cannot_replace_canonical(database, records):
    original_raw = archive(database, records)
    first = save(database, observation(records, original_raw, START, net=7))
    identical_raw = archive(database, records, at=SEEN + timedelta(minutes=1))
    identical = save(database, observation(records, identical_raw, START, net=7))
    assert first.revision_id == identical.revision_id
    with client_for(database) as client:
        point = client.get(path(records, "/latest")).json()["point"]
        assert point["provenance"]["revision_id"] == str(first.revision_id)
        assert point["provenance"]["raw_id"] == str(identical_raw.id)
        assert point["timing"]["first_seen_at"].replace("Z", "+00:00") == SEEN.isoformat()
        assert point["timing"]["last_seen_at"].replace("Z", "+00:00") == identical_raw.fetched_at.isoformat()
        corrected_raw = archive(database, records, at=SEEN + timedelta(minutes=2))
        publication = SEEN - timedelta(hours=1)
        corrected = save(database, observation(records, corrected_raw, START, net=9, published=publication))
        assert corrected.revision_id != first.revision_id
        stale_raw = archive(database, records, at=SEEN - timedelta(minutes=1))
        stale = save(database, observation(records, stale_raw, START, net=1))
        assert not stale.canonical_changed
        current = client.get(path(records, "/latest")).json()["point"]
        assert component(current, "foreign_ex_dealers")["net_quantity"] == "9"
        assert current["provenance"]["revision_id"] == str(corrected.revision_id)
        assert current["provenance"]["raw_id"] == str(corrected_raw.id)
        assert current["timing"]["source_published_at"].replace("Z", "+00:00") == publication.isoformat()
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InstitutionalReportRecord)) == 1
        assert session.scalar(select(func.count()).select_from(InstitutionalReportRevisionRecord)) == 3
        assert session.scalar(select(func.count()).select_from(InstitutionalIngestionItemRecord)) == 4


def test_repeatable_read_snapshot_survives_overlapping_committed_revision(database, records):
    raw = archive(database, records)
    original = save(database, observation(records, raw, START, net=7))
    with Session(database.execution_options(isolation_level="REPEATABLE READ")) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        service = query_service(session)
        first = service.latest(records.instrument.id)["point"]
        # This second transaction commits while the first snapshot stays open.
        correction_raw = archive(database, records, at=SEEN + timedelta(minutes=1))
        correction = save(database, observation(records, correction_raw, START, net=21))
        same_snapshot = service.latest(records.instrument.id)["point"]
        assert first == same_snapshot
        assert first["provenance"]["revision_id"] == original.revision_id
    with Session(database) as session:
        later = query_service(session).latest(records.instrument.id)["point"]
        assert later["provenance"]["revision_id"] == correction.revision_id
        assert component(later, "foreign_ex_dealers")["net_quantity"] == "21"


def test_real_api_dependency_enforces_readonly_repeatable_snapshot(database, records):
    raw = archive(database, records)
    save(database, observation(records, raw, START))
    transaction_modes = []
    def inspect_mode(connection, cursor, statement, parameters, context, executemany):
        if "institutional_reports" in statement and statement.lstrip().upper().startswith("SELECT"):
            with cursor.connection.cursor() as probe:
                probe.execute("SHOW transaction_isolation")
                isolation = probe.fetchone()[0]
                probe.execute("SHOW transaction_read_only")
                transaction_modes.append((isolation, probe.fetchone()[0]))
    app = create_app()
    with TestClient(app) as client:
        app.state.market_query_engine = database
        event.listen(database, "before_cursor_execute", inspect_mode)
        try:
            response = client.get(path(records), params=parameters())
        finally:
            event.remove(database, "before_cursor_execute", inspect_mode)
    assert response.status_code == 200
    assert transaction_modes and set(transaction_modes) == {("repeatable read", "on")}


def test_get_uses_canonical_only_without_provider_requests(database, records, monkeypatch):
    series(database, records)
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    async def forbidden_request(*args, **kwargs):
        pytest.fail("A query GET must not fetch a provider")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_request)
    event.listen(database, "before_cursor_execute", capture)
    try:
        with client_for(database, confirmed=True) as client:
            for suffix in ("", "/latest", "/metrics"):
                response = client.get(path(records, suffix), params=parameters() if suffix != "/latest" else None)
                assert response.status_code == 200
    finally:
        event.remove(database, "before_cursor_execute", capture)
    assert statements
    assert not any("institutional_raw_payloads" in statement for statement in statements)
    assert not any(statement.lstrip().startswith(("insert", "update", "delete")) for statement in statements)


def test_status_distinguishes_canonical_and_unverified_candidates(database, records):
    raw = archive(database, records)
    save(database, observation(records, raw, START))
    save(database, observation(records, raw, SESSIONS[1],
        scope=replace(SCOPE, includes_block_trades=None), row_index=1), row_index=1)
    with client_for(database) as client:
        response = client.get("/api/v1/chip-data/status", params={
            "product": "institutional_daily", "market": "TWSE", "source_code": records.source.code})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1 and body["is_realtime"] is False
        status = body["items"][0]
        assert status["canonical_count"] == status["candidate_count"] == 1
        assert status["latest_canonical_date"] == START.isoformat()
        assert status["latest_reported_as_of_date"] == SESSIONS[1].isoformat()
        assert status["latest_run"]["id"] == str(raw.run_id)
        assert status["coverage"]["completeness"] == "not_verified"
        assert status["capabilities"]["automated_ingestion_enabled"] is False
        first = client.get("/api/v1/chip-data/status", params={"limit": 1}).json()
        assert first["next_cursor"]
        second = client.get("/api/v1/chip-data/status", params={"limit": 1, "cursor": first["next_cursor"]})
        assert second.status_code == 200
        assert second.json()["items"][0]["key"] != first["items"][0]["key"]


def test_missing_private_schema_table_returns_safe_503(database, records):
    # Failure injection is confined to the per-test random schema.
    with database.begin() as connection:
        connection.execute(text("ALTER TABLE institutional_reports RENAME TO synthetic_unavailable_reports"))
    try:
        with client_for(database) as client:
            response = client.get(path(records), params=parameters())
        assert response.status_code == 503
        body = response.json()
        assert body["detail"]["code"] == "database_unavailable"
        assert "sql" not in response.text.lower() and "postgresql" not in response.text.lower()
        assert "synthetic_unavailable" not in response.text and "password" not in response.text.lower()
    finally:
        with database.begin() as connection:
            connection.execute(text("ALTER TABLE synthetic_unavailable_reports RENAME TO institutional_reports"))
