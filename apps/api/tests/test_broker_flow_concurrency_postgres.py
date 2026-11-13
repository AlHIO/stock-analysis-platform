"""SYNTHETIC concurrent broker facts in a disposable PostgreSQL schema only.

Events establish old transaction snapshots and pending writes. PostgreSQL's
actual blocking graph, rather than a sleep, proves that the conflicting
statement reached its database lock before the first transaction commits.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import os
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stockapp.modules.broker_flow.domain.models import BrokerBranch
from stockapp.modules.broker_flow.infrastructure.models import IDENTITIES
from stockapp.modules.broker_flow.infrastructure.repositories import (
    SqlAlchemyBrokerFlowRepository, SqlAlchemyBrokerIdentityRepository,
)
from test_broker_flow_postgres import DAY, archive, counts, get, refs, report
from test_market_data_postgres import database


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="Explicit disposable PostgreSQL required")]

ISOLATION_LEVELS = ("READ COMMITTED", "REPEATABLE READ")
EVENT_TIMEOUT = 8
FUTURE_TIMEOUT = 15


def _sqlstate(error):
    return getattr(error.orig, "sqlstate", None)


def _wait_for_blocker(engine, *, waiter_pid, blocker_pid):
    """Observe a real row/advisory lock dependency, with a bounded deadline."""
    deadline = monotonic() + EVENT_TIMEOUT
    with engine.connect() as observer:
        while monotonic() < deadline:
            blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})
            if blocker_pid in blockers:
                return
    pytest.fail("SYNTHETIC contender never reached the expected PostgreSQL lock")


def _close_with_direct_sql(session, identity_id):
    """Avoid a repository SELECT FOR UPDATE masking a missing trigger guard."""
    session.execute(update(IDENTITIES).where(IDENTITIES.c.id == identity_id).values(
        valid_to=DAY, closure_evidence="SYNTHETIC concurrent closure evidence",
        closed_at=datetime.now(timezone.utc), closure_event_kind="closure",
        closure_successor_branch_id=None))


@pytest.mark.parametrize("isolation_level", ISOLATION_LEVELS)
def test_concurrent_closure_cannot_invalidate_a_newly_committed_report(database, refs, isolation_level):
    value = report(refs, archive(database, refs))
    snapshot_ready, allow_closure, statement_started = Event(), Event(), Event()
    worker_state = {}

    def close_from_old_snapshot():
        try:
            with database.connect().execution_options(isolation_level=isolation_level) as connection:
                with Session(bind=connection) as session, session.begin():
                    worker_state["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
                    original = session.execute(select(IDENTITIES).where(
                        IDENTITIES.c.id == refs.identities[0].id)).mappings().one()
                    assert original["valid_to"] is None and original["protected_through"] is None
                    snapshot_ready.set()
                    assert allow_closure.wait(EVENT_TIMEOUT), "SYNTHETIC report writer did not release contender"
                    statement_started.set()
                    _close_with_direct_sql(session, refs.identities[0].id)
            return "unexpected_commit"
        except DBAPIError as error:
            return _sqlstate(error)

    with ThreadPoolExecutor(max_workers=1) as pool:
        contender = pool.submit(close_from_old_snapshot)
        try:
            assert snapshot_ready.wait(EVENT_TIMEOUT), "SYNTHETIC closure snapshot was not established"
            with Session(database) as writer, writer.begin():
                writer_pid = writer.scalar(text("SELECT pg_backend_pid()"))
                written = SqlAlchemyBrokerFlowRepository(writer).upsert(value, row_index=0)
                assert written.decision.value == "promoted"
                allow_closure.set()
                assert statement_started.wait(EVENT_TIMEOUT)
                _wait_for_blocker(database, waiter_pid=worker_state["pid"], blocker_pid=writer_pid)
            assert contender.result(timeout=FUTURE_TIMEOUT) in {"23514", "40001"}
        finally:
            allow_closure.set()

    actual = get(database, value)
    assert actual is not None and actual.revision_id == written.revision_id
    assert actual.trades == value.trades
    assert counts(database) == (1, 1, 2, 1, 1)
    with Session(database) as session:
        mapping = session.execute(select(IDENTITIES).where(
            IDENTITIES.c.id == refs.identities[0].id)).mappings().one()
        assert mapping["valid_to"] is None
        assert mapping["protected_through"] == value.trade_date


@pytest.mark.parametrize("isolation_level", ISOLATION_LEVELS)
def test_report_from_old_snapshot_cannot_outlive_a_concurrently_committed_closure(database, refs, isolation_level):
    value = report(refs, archive(database, refs))
    snapshot_ready, allow_report, statement_started = Event(), Event(), Event()
    worker_state = {}

    def write_from_old_snapshot():
        try:
            with database.connect().execution_options(isolation_level=isolation_level) as connection:
                with Session(bind=connection) as session, session.begin():
                    worker_state["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
                    assert session.scalar(select(IDENTITIES.c.valid_to).where(
                        IDENTITIES.c.id == refs.identities[0].id)) is None
                    snapshot_ready.set()
                    assert allow_report.wait(EVENT_TIMEOUT), "SYNTHETIC closure writer did not release contender"
                    statement_started.set()
                    SqlAlchemyBrokerFlowRepository(session).upsert(value, row_index=0)
            return "unexpected_commit"
        except DBAPIError as error:
            return _sqlstate(error)

    with ThreadPoolExecutor(max_workers=1) as pool:
        contender = pool.submit(write_from_old_snapshot)
        try:
            assert snapshot_ready.wait(EVENT_TIMEOUT), "SYNTHETIC report snapshot was not established"
            with Session(database) as closer, closer.begin():
                closer_pid = closer.scalar(text("SELECT pg_backend_pid()"))
                _close_with_direct_sql(closer, refs.identities[0].id)
                allow_report.set()
                assert statement_started.wait(EVENT_TIMEOUT)
                _wait_for_blocker(database, waiter_pid=worker_state["pid"], blocker_pid=closer_pid)
            assert contender.result(timeout=FUTURE_TIMEOUT) in {"23514", "40001"}
        finally:
            allow_report.set()

    assert get(database, value) is None
    assert counts(database) == (1, 0, 0, 0, 0)
    with Session(database) as session:
        mapping = session.execute(select(IDENTITIES).where(
            IDENTITIES.c.id == refs.identities[0].id)).mappings().one()
        assert mapping["valid_to"] == DAY
        assert mapping["protected_through"] is None


@pytest.mark.parametrize("isolation_level", ISOLATION_LEVELS)
@pytest.mark.parametrize("conflict_kind", ["code", "branch"])
def test_concurrent_overlapping_identity_maps_are_rejected_even_from_old_snapshot(
        database, refs, isolation_level, conflict_kind):
    with Session(database) as session, session.begin():
        registry = SqlAlchemyBrokerIdentityRepository(session)
        first_branch = BrokerBranch(id=uuid4(), broker_id=refs.broker.id)
        second_branch = BrokerBranch(id=uuid4(), broker_id=refs.broker.id)
        registry.add_branch(first_branch)
        registry.add_branch(second_branch)
    first = replace(refs.identities[0], id=uuid4(), branch_id=first_branch.id,
                    branch_code="SYNTHETIC-concurrent-code")
    second = replace(first, id=uuid4(), identity_version="SYNTHETIC-competing-v2",
        branch_id=second_branch.id if conflict_kind == "code" else first_branch.id,
        branch_code=first.branch_code if conflict_kind == "code" else "SYNTHETIC-other-code")
    snapshot_ready, allow_mapping, statement_started = Event(), Event(), Event()
    worker_state = {}

    def insert_from_old_snapshot():
        try:
            with database.connect().execution_options(isolation_level=isolation_level) as connection:
                with Session(bind=connection) as session, session.begin():
                    worker_state["pid"] = session.scalar(text("SELECT pg_backend_pid()"))
                    assert session.scalar(select(func.count()).select_from(IDENTITIES)) == 2
                    snapshot_ready.set()
                    assert allow_mapping.wait(EVENT_TIMEOUT), "SYNTHETIC mapping writer did not release contender"
                    statement_started.set()
                    SqlAlchemyBrokerIdentityRepository(session).add_identity(second)
            return "unexpected_commit"
        except DBAPIError as error:
            return _sqlstate(error)

    with ThreadPoolExecutor(max_workers=1) as pool:
        contender = pool.submit(insert_from_old_snapshot)
        try:
            assert snapshot_ready.wait(EVENT_TIMEOUT), "SYNTHETIC mapping snapshot was not established"
            with Session(database) as writer, writer.begin():
                writer_pid = writer.scalar(text("SELECT pg_backend_pid()"))
                SqlAlchemyBrokerIdentityRepository(writer).add_identity(first)
                allow_mapping.set()
                assert statement_started.wait(EVENT_TIMEOUT)
                _wait_for_blocker(database, waiter_pid=worker_state["pid"], blocker_pid=writer_pid)
            assert contender.result(timeout=FUTURE_TIMEOUT) in {"23514", "23P01", "40001"}
        finally:
            allow_mapping.set()

    with Session(database) as session:
        identities = session.execute(select(IDENTITIES)).mappings().all()
    assert len(identities) == 3
    assert any(identity["id"] == first.id for identity in identities)
    assert not any(identity["id"] == second.id for identity in identities)
