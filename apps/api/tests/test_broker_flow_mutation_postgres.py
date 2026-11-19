"""SYNTHETIC immutable-fact SQL mutation matrix in private PostgreSQL schemas.

No registry status or controlled identity-closure column is classified as an
immutable fact here. Real ingestion seeds every tested table before mutation.
"""
from datetime import timedelta
from hashlib import sha256
import os
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stockapp.modules.broker_flow.domain.models import BrokerMetricDefinition, MetricKind
from stockapp.modules.broker_flow.infrastructure.ingestion_models import GROUPS, JOBS, ROWS
from stockapp.modules.broker_flow.infrastructure.ingestion_store import SqlAlchemyBrokerImportStore
from stockapp.modules.broker_flow.infrastructure.models import (
    AUDITS, CANONICAL, ITEMS, METRICS, RAW, REVISIONS, SCOPES, TRADES,
)
from stockapp.modules.broker_flow.infrastructure.repositories import SqlAlchemyBrokerFlowRepository
from test_broker_flow_ingestion_postgres import contract_for, csv_bytes, import_bytes, source_rows
from test_broker_flow_postgres import DAY, refs  # noqa: F401 - private-schema reference fixture
from test_market_data_postgres import database  # noqa: F401 - opt-in disposable schema fixture


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="Explicit disposable PostgreSQL required")]

IMMUTABLE_FACTS = (AUDITS, RAW, SCOPES, REVISIONS, TRADES, ITEMS, METRICS, GROUPS, ROWS)
SNAPSHOT_TABLES = (*IMMUTABLE_FACTS, CANONICAL, JOBS)


@pytest.fixture
def seeded_facts(database, refs):
    """Produce a legitimate Raw→Audit→Revision→Canonical import before attacks."""
    body = csv_bytes(source_rows(refs))
    outcome = import_bytes(SqlAlchemyBrokerImportStore(database), contract_for(refs), body)
    assert outcome["status"] == "succeeded"
    assert outcome["stats"]["accepted_reports"] == 1
    with Session(database) as session, session.begin():
        SqlAlchemyBrokerFlowRepository(session).add_metric_definition(BrokerMetricDefinition(
            algorithm_id="SYNTHETIC.sql_mutation_guard", version="1.0.0",
            name="SYNTHETIC immutable definition", kind=MetricKind.DERIVED,
            formula="SYNTHETIC exact source net", output_unit="shares"))
    return dict(raw_id=UUID(outcome["raw_id"]), run_id=UUID(outcome["run_id"]), body=body)


def fact_snapshot(engine):
    """Compare full contents, not only row counts or a trigger error message."""
    with Session(engine) as session:
        return {table.name: [dict(row) for row in session.execute(select(table).order_by(
            *table.primary_key.columns)).mappings()] for table in SNAPSHOT_TABLES}


def assert_lineage_unchanged(engine, refs, seeded, before):
    assert fact_snapshot(engine) == before
    with Session(engine) as session:
        raw = session.execute(select(RAW).where(RAW.c.id == seeded["raw_id"])).mappings().one()
        assert raw["body"] == seeded["body"]
        assert raw["sha256"] == sha256(seeded["body"]).hexdigest()
        pointer = session.execute(select(CANONICAL).where(CANONICAL.c.instrument_id == refs.instrument.id,
            CANONICAL.c.trade_date == DAY, CANONICAL.c.scope_id == refs.scope.id)).mappings().one()
        observation = session.execute(select(ITEMS).where(ITEMS.c.id == pointer["observation_id"])).mappings().one()
        revision = session.execute(select(REVISIONS).where(REVISIONS.c.id == pointer["revision_id"])).mappings().one()
        assert observation["revision_id"] == revision["id"]
        assert observation["raw_id"] == revision["first_raw_id"] == seeded["raw_id"]
        assert observation["run_id"] == revision["first_run_id"] == seeded["run_id"]
        assert observation["report_json"]["lineage"]["payload_sha256"] == raw["sha256"]
        assert len(observation["report_json"]["trades"]) == revision["branch_count"] == 2


def changed_values(table, row):
    """Use valid field types and ordinary non-key changes, not malformed SQL."""
    if table is AUDITS:
        return dict(manifest_version="SYNTHETIC-mutated-manifest-v2")
    if table is RAW:
        return dict(content_type="text/x-synthetic-mutated")
    if table is SCOPES:
        return dict(scope_json=dict(row["scope_json"]) | {"description": "SYNTHETIC changed scope description"})
    if table is REVISIONS:
        return dict(created_at=row["created_at"] + timedelta(seconds=1))
    if table is TRADES:
        return dict(row_locators=["SYNTHETIC/mutated-source-row"])
    if table is ITEMS or table is ROWS:
        return dict(reason="SYNTHETIC changed audit reason")
    if table is METRICS:
        return dict(definition_json=dict(row["definition_json"]) | {"name": "SYNTHETIC changed definition"})
    if table is GROUPS:
        return dict(created_at=row["created_at"] + timedelta(seconds=1))
    raise AssertionError("Untested mutable registry table must not enter this matrix")


@pytest.mark.parametrize("table", IMMUTABLE_FACTS, ids=lambda table: table.name)
def test_direct_update_of_seeded_immutable_fact_is_rejected_and_rolled_back(database, refs, seeded_facts, table):
    before = fact_snapshot(database)
    assert before[table.name], "Each immutable target needs a real saved fixture row"
    original = before[table.name][0]
    selector = [column == original[column.name] for column in table.primary_key.columns]
    with pytest.raises(DBAPIError) as error:
        with Session(database) as session, session.begin():
            session.execute(update(table).where(*selector).values(**changed_values(table, original)))
    # A missing privilege or invalid TRUNCATE dependency is not proof of a guard.
    assert getattr(error.value.orig, "sqlstate", None) == "23514"
    assert_lineage_unchanged(database, refs, seeded_facts, before)


@pytest.mark.parametrize("table", IMMUTABLE_FACTS, ids=lambda table: table.name)
def test_truncate_cascade_of_seeded_immutable_fact_is_rejected_and_rolled_back(database, refs, seeded_facts, table):
    before = fact_snapshot(database)
    assert before[table.name], "An empty table cannot prove preserved imported facts"
    # Names originate only from the fixed ORM table matrix. CASCADE avoids a
    # foreign-key precondition being mistaken for the BEFORE TRUNCATE guard.
    with pytest.raises(DBAPIError) as error:
        with Session(database) as session, session.begin():
            session.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
    assert getattr(error.value.orig, "sqlstate", None) == "23514"
    assert_lineage_unchanged(database, refs, seeded_facts, before)
