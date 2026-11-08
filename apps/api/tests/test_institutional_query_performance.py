"""SYNTHETIC bounded query and index checks in disposable PostgreSQL schemas.

Plans from small fixtures are not production performance claims. The explicit
seqscan-off probe only verifies index eligibility; the natural plan is recorded
separately and is allowed to prefer a small-table sequential scan.
"""
from dataclasses import replace
from datetime import timedelta
import json
import os

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_institutional_query_postgres import (
    START, SCOPE, archive, observation, records, save, series,
)
from stockapp.modules.institutional.domain.persistence import scope_values
from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
from stockapp.modules.institutional.infrastructure.repositories import CANONICAL

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Dedicated PostgreSQL test target required")]


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


def _seed(database, records):
    series(database, records)
    series(database, replace(records, instrument=records.empty))
    raw = archive(database, records)
    save(database, observation(records, raw, START + timedelta(days=20),
        net=9999, scope=replace(SCOPE, includes_block_trades=False)))


def test_history_plan_uses_available_instrument_date_scope_index(database, records):
    _seed(database, records)
    with database.begin() as connection:
        constraints = inspect(connection).get_unique_constraints("institutional_reports")
        natural = next(item for item in constraints if item["name"] == "uq_inst_report_natural_key")
        assert natural["column_names"] == ["instrument_id", "trade_date", "scope"]
        connection.execute(text("ANALYZE institutional_reports"))
        explain = text("""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT * FROM institutional_reports
            WHERE instrument_id = :instrument_id AND scope = CAST(:scope AS jsonb)
              AND trade_date >= :start AND trade_date <= :end
            ORDER BY trade_date ASC LIMIT :limit""")
        parameters = dict(instrument_id=records.instrument.id, scope=json.dumps(scope_values(SCOPE)),
            start=START + timedelta(days=20), end=START + timedelta(days=22), limit=2)
        natural_plan = connection.scalar(explain, parameters)[0]
        assert natural_plan["Plan"]["Actual Rows"] == 2
        print("Synthetic natural history plan:", json.dumps(natural_plan, default=str))
        # A tiny fixture may rationally choose a sequential scan. Do not turn
        # planner cost estimates into a flaky test or claim this is a benchmark.
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        eligible = connection.scalar(explain, parameters)[0]
        indexes = {node.get("Index Name") for node in _nodes(eligible["Plan"])}
        assert indexes & {"uq_inst_report_natural_key", "ix_inst_report_date_instrument"}
        assert eligible["Plan"]["Actual Rows"] == 2
        print("Synthetic index-eligibility plan:", json.dumps(eligible, default=str))


def test_repository_page_is_bounded_and_never_loads_other_instruments(database, records):
    _seed(database, records)
    queries = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement.lower(), parameters))
    event.listen(database, "before_cursor_execute", capture)
    try:
        with Session(database) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            repository = SqlAlchemyInstitutionalQueryRepository(session)
            reports = repository.page(records.instrument.id, SCOPE,
                START + timedelta(days=20), START + timedelta(days=22), limit=2)
    finally:
        event.remove(database, "before_cursor_execute", capture)
    assert len(reports) == 2
    assert all(report.instrument_id == records.instrument.id and report.scope == SCOPE for report in reports)
    assert [report.trade_date for report in reports] == [START + timedelta(days=20), START + timedelta(days=21)]
    page_sql, parameters = queries[0]
    assert "institutional_reports.instrument_id =" in page_sql
    assert "institutional_reports.scope =" in page_sql
    assert "institutional_reports.trade_date >=" in page_sql and "institutional_reports.trade_date <=" in page_sql
    assert "order by institutional_reports.trade_date asc" in page_sql and "limit" in page_sql
    assert 2 in parameters.values() and records.instrument.id in parameters.values()
    assert not any("institutional_raw_payloads" in statement for statement, _ in queries)
    # Record current finite reconstruction work; future batch optimization may
    # reduce this count, but may never grow beyond four indexed reads per point.
    assert len(queries) <= 1 + 4 * len(reports)
    with Session(database) as session:
        all_rows = session.execute(select(CANONICAL.c.id)).all()
    assert len(all_rows) == 121
    print("Synthetic page returned", len(reports), "of", len(all_rows), "rows using", len(queries), "SELECTs")
