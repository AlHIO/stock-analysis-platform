"""SYNTHETIC Phase 2B full-flow acceptance in private PostgreSQL schemas.

These examples are software validation only, never real source coverage. TWSE
uses legally marked offline fixture bytes and its real parser, not a network
permission override. TPEx additionally exercises its real adapter/HTTP transport
through httpx.MockTransport. Production source manifests remain unchanged.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import os
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_institutional_ingestion_postgres import (
    DAY, OBSERVED, FixtureProvider, counts, fixture_body, seed,
)
from test_institutional_query_postgres import client_for, component
from stockapp.modules.institutional.application.ingestion import InstitutionalIngestionService
from stockapp.modules.institutional.infrastructure.ingestion_store import SqlAlchemyInstitutionalIngestionStore
from stockapp.modules.institutional.infrastructure.models import (
    InstitutionalIngestionItemRecord as ItemRecord,
    InstitutionalRawPayloadRecord as RawRecord,
    InstitutionalReportRecord as CanonicalRecord,
    InstitutionalReportRevisionRecord as RevisionRecord,
)
from stockapp.modules.institutional.infrastructure.operations import RuntimeProvider, source_spec
from stockapp.modules.institutional.infrastructure.providers import TpexInstitutionalAdapter
from stockapp.modules.institutional.infrastructure.transport import HostRateLimiter, InstitutionalHttpTransport
from stockapp.modules.market_data.domain.models import DailyBar, DataSource, SourceType
from stockapp.modules.market_data.infrastructure.models import DailyBarRecord, DailyBarRevisionRecord
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Isolated PostgreSQL acceptance target required")]


def offline_source(provider="twse"):
    """Real API-supported market codes, synthetic names/identities in test schema."""
    source = source_spec(provider)
    return replace(source, market_name="SYNTHETIC E2E market, not production data",
        source_name="SYNTHETIC E2E source, not real coverage",
        authorization="synthetic_test_approved" if provider == "twse" else source.authorization)


def ingest(database, *, provider=None, source=None, evidence=None, store=None):
    source = source or offline_source()
    evidence = seed(database, source) if evidence is None else evidence
    store = store or SqlAlchemyInstitutionalIngestionStore(database, host_interval=0)
    service = InstitutionalIngestionService(store, source, provider or FixtureProvider(source.provider))
    result = asyncio.run(service.sync(trade_date=DAY if source.provider == "twse" else None, evidence=evidence))
    return result, service, store, UUID(evidence[0]["instrument_id"])


def latest(client, identifier):
    response = client.get(f"/api/v1/stocks/{identifier}/institutional/latest")
    assert response.status_code == 200, response.text
    return response.json()["point"]


def seed_daily_sentinel(database, identifier):
    """A Phase 1 bar and its immutable revision must survive the chip pipeline."""
    with Session(database) as session, session.begin():
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code="SYNTH_E2E_DAILY", name="Synthetic DailyBar sentinel", source_type=SourceType.MANUAL))
        SqlAlchemyDailyBarRepository(session).upsert(DailyBar(instrument_id=identifier, trade_date=DAY,
            open=Decimal("10.01"), high=Decimal("10.03"), low=Decimal("10.00"), close=Decimal("10.02"),
            volume_shares=1234, turnover=Decimal("12364.68"), trade_count=12, source_id=source.id,
            ingested_at=OBSERVED))


def prices(database):
    with Session(database) as session:
        return (tuple(dict(row) for row in session.execute(select(DailyBarRecord.__table__)).mappings()),
            tuple(dict(row) for row in session.execute(select(DailyBarRevisionRecord.__table__)).mappings()))


def test_offline_twse_provider_to_api_exact_values_lineage_and_no_get_network(database, monkeypatch):
    source = offline_source()
    evidence = seed(database, source)
    identifier = UUID(evidence[0]["instrument_id"])
    seed_daily_sentinel(database, identifier)
    original_prices = prices(database)
    provider = FixtureProvider()
    result, _, _, _ = ingest(database, source=source, evidence=evidence, provider=provider)
    assert result["status"] == "succeeded" and result["canonical_promotions"] == 1
    assert counts(database) == (1, 1, 1, 1)
    with Session(database) as session:
        raw = session.get(RawRecord, UUID(result["selected_raw_id"]))
        canonical = session.scalar(select(CanonicalRecord))
        revision = session.get(RevisionRecord, canonical.revision_id)
        audit = session.get(ItemRecord, canonical.observation_id)
        assert raw.body == fixture_body() and raw.sha256 == sha256(fixture_body()).hexdigest()
        assert raw.run_id == audit.run_id == UUID(result["run_id"])
        assert audit.raw_id == raw.id and audit.revision_id == revision.id
        assert revision.foreign_ex_dealers_buy_quantity == 1000
        assert revision.foreign_ex_dealers_sell_quantity == 400
        assert revision.foreign_ex_dealers_net_quantity == 600
        assert revision.dealer_proprietary_net_quantity == 50 and revision.dealer_hedging_net_quantity == -100
        assert revision.three_major_total_net_quantity == 750
        revision_id, raw_id, normalized_hash = revision.id, raw.id, revision.payload_hash
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    async def forbidden(*args, **kwargs):
        pytest.fail("Institutional GET attempted a provider/network request")
    monkeypatch.setattr(RuntimeProvider, "fetch", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    event.listen(database, "before_cursor_execute", capture)
    try:
        with client_for(database) as client:
            point = latest(client, identifier)
            assert component(point, "foreign_ex_dealers")["buy_quantity"] == "1000"
            assert component(point, "foreign_ex_dealers")["sell_quantity"] == "400"
            assert component(point, "foreign_ex_dealers")["net_quantity"] == "600"
            assert point["provenance"]["revision_id"] == str(revision_id)
            assert point["provenance"]["raw_id"] == str(raw_id)
            assert point["provenance"]["normalized_hash"] == normalized_hash
            assert point["trade_date"] == DAY.isoformat() and point["quantity_unit"] == "shares"
            assert point["timing"]["source_published_at"] is None
            totals = {item["category"]:item["value"] for item in point["derived_daily"]}
            assert totals["dealer_total"] == "-50" and totals["three_major_total"] == "750"
            base = f"/api/v1/stocks/{identifier}/institutional"
            history = client.get(base, params={"from":str(DAY),"to":str(DAY)}).json()
            assert history["count"] == 1 and history["items"][0] == point
            metrics = client.get(base+"/metrics", params={"from":str(DAY),"to":str(DAY)}).json()
            assert metrics["daily"]["value"] == "600" and metrics["calendar"]["verified"] is False
            status = client.get("/api/v1/chip-data/status", params={"market":"TWSE"}).json()["items"][0]
            assert status["canonical_count"] == 1 and status["latest_run"]["counts"]["accepted"] == 1
    finally:
        event.remove(database, "before_cursor_execute", capture)
    assert provider.fetches == 1 and statements
    assert not any("institutional_raw_payloads" in statement for statement in statements)
    assert not any(statement.lstrip().startswith(("insert", "update", "delete")) for statement in statements)
    assert prices(database) == original_prices


def test_repeated_ingestion_revision_and_old_replay_match_api_and_database(database):
    original, service, _, identifier = ingest(database)
    with client_for(database) as client:
        first = latest(client, identifier)
        ingest(database, provider=FixtureProvider(at=OBSERVED + timedelta(minutes=1)))
        same = latest(client, identifier)
        assert same["provenance"]["revision_id"] == first["provenance"]["revision_id"]
        assert counts(database) == (2, 1, 2, 1)
        corrected, _, _, _ = ingest(database,
            provider=FixtureProvider(body=fixture_body(net=700), at=OBSERVED + timedelta(minutes=2)))
        newer = latest(client, identifier)
        assert component(newer, "foreign_ex_dealers")["net_quantity"] == "700"
        assert newer["provenance"]["revision_id"] != first["provenance"]["revision_id"]
        assert newer["provenance"]["run_id"] == corrected["run_id"]
        replay = asyncio.run(service.replay(UUID(original["selected_raw_id"])))
        assert replay["stats"]["stale"] == 1 and replay["canonical_promotions"] == 0
        assert latest(client, identifier) == newer
    assert counts(database) == (4, 2, 4, 1)
    with Session(database) as session:
        canonical = session.scalar(select(CanonicalRecord))
        revision = session.get(RevisionRecord, canonical.revision_id)
        assert str(revision.id) == newer["provenance"]["revision_id"]
        assert revision.foreign_ex_dealers_net_quantity == 700


def test_transaction_failure_preserves_previous_api_point_and_resume_is_atomic(database, monkeypatch):
    _, _, _, identifier = ingest(database)
    store = SqlAlchemyInstitutionalIngestionStore(database, host_interval=0)
    source = offline_source()
    evidence = seed(database, source)
    provider = FixtureProvider(body=fixture_body(net=800), at=OBSERVED + timedelta(minutes=3))
    service = InstitutionalIngestionService(store, source, provider)
    advance = store._advance
    def fail_after_checkpoint(session, row, stats):
        advance(session, row, stats)
        raise RuntimeError("SYNTHETIC E2E interruption before report transaction commit")
    monkeypatch.setattr(store, "_advance", fail_after_checkpoint)
    with client_for(database) as client:
        original = latest(client, identifier)
        with pytest.raises(RuntimeError, match="SYNTHETIC E2E"):
            asyncio.run(service.sync(trade_date=DAY, evidence=evidence))
        pending = next(row for row in store.inspect() if row["status"] == "running")
        assert pending["next_row_index"] == pending["stats"]["processed"] == 0
        assert counts(database) == (2, 1, 1, 1)
        assert latest(client, identifier) == original
        monkeypatch.setattr(store, "_advance", advance)
        resumed = asyncio.run(service.resume(UUID(pending["run_id"])))
        assert resumed["status"] == "succeeded" and provider.fetches == 1
        assert component(latest(client, identifier), "foreign_ex_dealers")["net_quantity"] == "800"
    assert counts(database) == (2, 2, 2, 1)


def test_rejected_stock_has_audit_while_valid_stock_reaches_api(database):
    source = offline_source()
    evidence = seed(database, source, ("SYNTH1", "SYNTH2"))
    result, _, _, _ = ingest(database, source=source, evidence=evidence,
        provider=FixtureProvider(body=fixture_body(rows=2, invalid_first=True)))
    assert result["status"] == "partial"
    assert result["stats"]["accepted"] == result["stats"]["rejected"] == 1
    assert result["issues"][0]["reason"] == "invalid_quantity"
    with client_for(database) as client:
        assert latest(client, UUID(evidence[0]["instrument_id"])) is None
        valid = latest(client, UUID(evidence[1]["instrument_id"]))
        assert component(valid, "foreign_ex_dealers")["net_quantity"] == "600"
        status = client.get("/api/v1/chip-data/status", params={"market":"TWSE"}).json()["items"][0]
        assert status["latest_run"]["has_partial_data"] is True
        assert status["latest_run"]["counts"]["rejected"] == 1
    assert counts(database) == (1, 1, 2, 1)


class SyntheticTpexHttpProvider:
    """The actual TPEx adapter and transport against an in-process HTTP fixture."""
    def __init__(self):
        self.requests = []
    def parse(self, raw, *, requested_date=None):
        return TpexInstitutionalAdapter().parse(raw, requested_date=requested_date)
    async def fetch(self, *, run_id, source_id, market_id, trade_date, on_response):
        def response(request):
            self.requests.append(request)
            return httpx.Response(200, content=fixture_body("tpex"), headers={"Content-Type":"application/json"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            async with InstitutionalHttpTransport(client=client, max_attempts=1, now=lambda:OBSERVED,
                limiter=HostRateLimiter(0, shared=False), on_response=on_response) as transport:
                async with TpexInstitutionalAdapter(network_enabled=True, transport=transport) as adapter:
                    return await adapter.fetch(run_id=run_id,source_id=source_id,market_id=market_id,trade_date=trade_date)


def test_tpex_actual_adapter_to_audit_revision_and_empty_canonical_api(database):
    provider = SyntheticTpexHttpProvider()
    result, _, _, identifier = ingest(database, source=offline_source("tpex"), provider=provider)
    assert len(provider.requests) == 1 and not provider.requests[0].url.query
    assert result["status"] == "partial"
    assert result["stats"]["accepted"] == result["stats"]["partial"] == result["stats"]["candidate"] == 1
    assert result["trade_date"] == DAY.isoformat() and result["canonical_promotions"] == 0
    assert counts(database) == (1, 1, 1, 0)
    with Session(database) as session:
        revision = session.scalar(select(RevisionRecord))
        assert revision.foreign_ex_dealers_buy_quantity == 1200
        assert revision.foreign_ex_dealers_sell_quantity == 200 and revision.foreign_ex_dealers_net_quantity == 1000
        assert revision.investment_trust_net_quantity == 200 and revision.dealer_total_net_quantity == 100
        assert revision.three_major_total_net_quantity == 1300
        assert revision.dealer_proprietary_net_quantity is None and revision.dealer_hedging_net_quantity is None
    with client_for(database) as client:
        assert latest(client, identifier) is None
        history = client.get(f"/api/v1/stocks/{identifier}/institutional", params={"from":str(DAY),"to":str(DAY)}).json()
        assert history["items"] == []
        status = client.get("/api/v1/chip-data/status", params={"market":"TPEX"}).json()["items"][0]
        assert status["candidate_count"] == 1 and status["canonical_count"] == 0
        assert "trade_scope_unverified" in status["capabilities"]["limitations"]
        assert status["latest_reported_as_of_date"] == DAY.isoformat()
        assert status["latest_run"]["counts"]["partial"] == 1
