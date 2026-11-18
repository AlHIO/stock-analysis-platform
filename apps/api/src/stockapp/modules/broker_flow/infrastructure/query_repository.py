"""Bounded Canonical broker reads in the caller's read-only transaction snapshot.

Source archives and frozen import configurations are deliberately not selected.
Identity, price and calendar evidence are never invented from observed activity.
"""
from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy import exists, func, select, tuple_

from stockapp.modules.market_data.public import market_data_reference_reader, market_data_runtime

from ..domain.analytics import BrokerAnalysisHistory, CanonicalBrokerInput
from ..domain.import_persistence import IMPORT_COUNTERS
from ..domain.models import BrokerFlowError
from ..domain.query import (
    BrokerQueryError, BrokerSelection, MAX_ANALYTICAL_LOOKBACK_DAYS,
    MAX_QUERY_REPORTS, MAX_QUERY_TRADES,
)
from .models import AUDITS, CANONICAL, IDENTITIES, ITEMS, RAW, REVISIONS, SCOPES, TRADES
from .repositories import identity_from_row, report_from_dict


def _stored_error():
    return BrokerQueryError("invalid_stored_broker_data", "Stored broker data is temporarily unavailable.", 503)


def _budget_error():
    return BrokerQueryError("broker_query_limit_exceeded", "Narrow the date range to at most 1000 reports and 20000 trades.")


class SqlAlchemyBrokerQueryRepository:
    """The caller owns transaction isolation, timeouts, lifecycle and read clock."""

    def __init__(self, session, *, calendar_reader=None, prices=None):
        self.session = session
        self.references = market_data_reference_reader(session)
        self.runtime = market_data_runtime(session)
        self.calendar_reader, self.prices = calendar_reader, prices
        self._sources = {}

    def _execute(self, statement):
        return self.session.execute(statement.execution_options(autoflush=False, populate_existing=True))

    def instrument(self, identifier):
        return self.references.instrument(identifier)

    def source(self, identifier):
        if identifier not in self._sources:
            value = self.references.source(identifier)
            if value is None:
                raise _stored_error()
            self._sources[identifier] = dict(value)
        return dict(self._sources[identifier])

    def _runtime_run(self, identifier):
        with self.session.no_autoflush:
            return self.runtime.get_run(identifier)

    @staticmethod
    def _select():
        # Read bounded metadata before loading a potentially large JSON report.
        return select(
            CANONICAL.c.instrument_id, CANONICAL.c.market_id, CANONICAL.c.trade_date,
            CANONICAL.c.scope_id, CANONICAL.c.source_id, CANONICAL.c.revision_id,
            CANONICAL.c.observation_id, CANONICAL.c.profile_hash,
            CANONICAL.c.observed_at, ITEMS.c.run_id, ITEMS.c.raw_id,
            ITEMS.c.ingested_at, ITEMS.c.promotion_policy_version,
            ITEMS.c.revision_id.label("item_revision_id"), ITEMS.c.source_id.label("item_source_id"),
            ITEMS.c.observed_at.label("item_observed_at"), ITEMS.c.disposition,
            REVISIONS.c.branch_count, REVISIONS.c.completeness,
            REVISIONS.c.branch_universe_complete, REVISIONS.c.quantity_unit,
            REVISIONS.c.source_namespace, REVISIONS.c.source_contract_version,
            REVISIONS.c.schema_version, REVISIONS.c.parser_version,
            REVISIONS.c.identity_policy_version, REVISIONS.c.price_basis,
            REVISIONS.c.price_quantity_basis, REVISIONS.c.currency,
            REVISIONS.c.source_published_at, REVISIONS.c.source_generated_at,
        ).select_from(CANONICAL).join(ITEMS, ITEMS.c.id == CANONICAL.c.observation_id).join(
            REVISIONS, REVISIONS.c.id == CANONICAL.c.revision_id)

    @staticmethod
    def _filters(statement, identifier, selection, *, broker=None, branch=None):
        statement = statement.where(CANONICAL.c.instrument_id == identifier)
        if selection.source is not None:
            statement = statement.where(CANONICAL.c.source_id == selection.source)
        if selection.trade_scope is not None:
            statement = statement.where(CANONICAL.c.scope_id == selection.trade_scope)
        if broker is not None or branch is not None:
            matching = select(1).select_from(TRADES).join(
                IDENTITIES, IDENTITIES.c.id == TRADES.c.identity_id).where(
                TRADES.c.revision_id == CANONICAL.c.revision_id)
            if branch is not None:
                matching = matching.where(TRADES.c.branch_id == branch)
            if broker is not None:
                matching = matching.where(IDENTITIES.c.broker_id == broker)
            statement = statement.where(exists(matching))
        return statement

    def _reports(self, rows):
        if not rows:
            return ()
        if len(rows) > MAX_QUERY_REPORTS + 1 or any(row["branch_count"] < 0 for row in rows):
            raise _budget_error()
        if sum(row["branch_count"] for row in rows) > MAX_QUERY_TRADES:
            raise _budget_error()
        # Budget checks happen before either report_json or identities are read.
        payloads = dict(self._execute(select(ITEMS.c.id, ITEMS.c.report_json).where(
            ITEMS.c.id.in_([row["observation_id"] for row in rows]))).all())
        revisions = {row["revision_id"] for row in rows}
        earliest = dict(self._execute(select(ITEMS.c.revision_id, func.min(ITEMS.c.observed_at)).where(
            ITEMS.c.revision_id.in_(revisions), ITEMS.c.disposition.in_(("accepted", "partial")))
            .group_by(ITEMS.c.revision_id)).all())
        try:
            typed = []
            identity_ids = set()
            for row in rows:
                report = report_from_dict(payloads[row["observation_id"]])
                if (report.instrument_id, report.market_id, report.trade_date, report.scope.id,
                    report.lineage.source_id, report.revision_id, report.lineage.run_id,
                    report.lineage.raw_payload_id, report.lineage.ingested_at,
                    report.quantity_unit.value, len(report.trades), report.completeness.value,
                    report.branch_universe_complete) != (
                    row["instrument_id"], row["market_id"], row["trade_date"], row["scope_id"],
                    row["source_id"], row["revision_id"], row["run_id"], row["raw_id"], row["ingested_at"],
                    row["quantity_unit"], row["branch_count"], row["completeness"], row["branch_universe_complete"]):
                    raise _stored_error()
                if (row["item_revision_id"], row["item_source_id"], row["item_observed_at"]) != (
                        row["revision_id"], row["source_id"], row["observed_at"]):
                    raise _stored_error()
                for key in ("source_namespace", "source_contract_version", "schema_version", "parser_version",
                            "identity_policy_version", "source_published_at", "source_generated_at"):
                    if getattr(report.lineage, key) != row[key]:
                        raise _stored_error()
                for key in ("price_basis", "price_quantity_basis", "currency"):
                    if getattr(report, key) != row[key]:
                        raise _stored_error()
                if row["disposition"] != "accepted":
                    raise _stored_error()
                report = replace(report, lineage=replace(report.lineage, first_seen_at=earliest[report.revision_id]))
                typed.append((row, report))
                identity_ids.update(trade.identity_id for trade in report.trades)
            identities = {entry["id"]: identity_from_row(entry) for entry in self._execute(
                select(IDENTITIES).where(IDENTITIES.c.id.in_(identity_ids))).mappings()} if identity_ids else {}
            result = []
            for row, report in typed:
                selected = tuple(identities[trade.identity_id] for trade in report.trades)
                # An exact dated mapping is required; never infer continuity or
                # an omitted no-activity row from a name, current registry or code.
                if any(not identity.verified or identity.branch_id != trade.branch_id
                    or identity.source_id != report.lineage.source_id
                    or identity.market_id != report.market_id
                    or identity.source_namespace != report.lineage.source_namespace
                    or not identity.effective_on(report.trade_date)
                    for trade, identity in zip(report.trades, selected)):
                    raise _stored_error()
                result.append(CanonicalBrokerInput(report=report, observation_id=row["observation_id"],
                    promotion_policy_version=row["promotion_policy_version"], identities=selected,
                    canonical_verified=True))
            return tuple(result)
        except (BrokerFlowError, ValueError, TypeError, KeyError) as exc:
            raise _stored_error() from exc

    def scopes(self, identifier, start, end, *, selection):
        statement = self._filters(select(CANONICAL.c.scope_id).where(
            CANONICAL.c.trade_date >= start, CANONICAL.c.trade_date <= end), identifier, selection)
        ids = self._execute(statement.distinct().order_by(CANONICAL.c.scope_id).limit(MAX_QUERY_REPORTS + 1)).scalars().all()
        if len(ids) > MAX_QUERY_REPORTS:
            raise _budget_error()
        rows = self._execute(select(SCOPES.c.id, SCOPES.c.source_id, SCOPES.c.market_id,
            SCOPES.c.code, SCOPES.c.version, SCOPES.c.scope_json).where(SCOPES.c.id.in_(ids))
            .order_by(SCOPES.c.id)).mappings().all() if ids else ()
        try:
            values = []
            for row in rows:
                value = dict(row["scope_json"])
                # SQL's typed identity fields are authoritative selector keys.
                for key in ("id", "source_id", "market_id", "code", "version"):
                    if str(value[key]) != str(row[key]):
                        raise _stored_error()
                values.append(value | {key: row[key] for key in ("id", "source_id", "market_id")})
            return tuple(values)
        except (ValueError, TypeError, KeyError) as exc:
            raise _stored_error() from exc

    def _page_metadata(self, identifier, start, end, *, selection, after=None, order="asc", limit=250,
                       broker=None, branch=None):
        if order not in {"asc", "desc"} or type(limit) is not int or not 1 <= limit <= MAX_QUERY_REPORTS + 1:
            raise _budget_error()
        statement = self._filters(self._select(), identifier, selection, broker=broker, branch=branch).where(
            CANONICAL.c.trade_date >= start, CANONICAL.c.trade_date <= end)
        keys = (CANONICAL.c.trade_date, CANONICAL.c.scope_id)
        if after is not None:
            statement = statement.where(tuple_(*keys) > after if order == "asc" else tuple_(*keys) < after)
        statement = statement.order_by(*(key.asc() if order == "asc" else key.desc() for key in keys)).limit(limit)
        return self._execute(statement).mappings().all()

    def page(self, identifier, start, end, *, selection, after=None, order="asc", limit=250,
             broker=None, branch=None):
        return self._reports(self._page_metadata(identifier, start, end, selection=selection,
            after=after, order=order, limit=limit, broker=broker, branch=branch))

    def latest(self, identifier, *, selection, broker=None, branch=None):
        rows = self.page(identifier, date.min, date.max, selection=selection, order="desc", limit=1,
                         broker=broker, branch=branch)
        return rows[0] if rows else None

    def extent(self, identifier, *, selection, broker=None, branch=None):
        statement = self._filters(select(func.min(CANONICAL.c.trade_date), func.max(CANONICAL.c.trade_date),
            func.count()), identifier, selection, broker=broker, branch=branch)
        return tuple(self._execute(statement).one())

    def load(self, request, *, selection=BrokerSelection()):
        start = request.start
        if request.metric in {"broker.branch_session_cumulative_net", "broker.branch_buy_streak", "broker.branch_sell_streak"}:
            start = max(date.min, request.end - timedelta(days=min(MAX_ANALYTICAL_LOOKBACK_DAYS,
                                                                 request.end.toordinal() - 1)))
        calendar = None
        if self.calendar_reader is not None:
            calendar = self.calendar_reader.load(market_id=request.market_id, start=start, end=request.end,
                                                  query_as_of=request.query_as_of)
        read_start = start
        if (request.metric == "broker.branch_session_cumulative_net" and calendar is not None
                and calendar.verified and calendar.market_id == request.market_id
                and calendar.timezone_name == request.timezone_name
                and calendar.start <= start and calendar.end >= request.end
                and calendar.observed_at is not None and calendar.observed_at <= request.query_as_of):
            dates = tuple(day for day in calendar.open_dates if day <= request.end)
            count = request.parameters.get("sessions", 5)
            if type(count) is int and count in {1, 5, 10, 20, 60} and len(dates) >= count:
                # Use the same verified calendar definition as the Domain;
                # earlier reports cannot affect an N-session cumulative value.
                read_start = dates[-count]
        metadata = self._page_metadata(request.instrument_id, read_start, request.end,
            selection=selection, order="asc", limit=MAX_QUERY_REPORTS + 1)
        if len(metadata) > MAX_QUERY_REPORTS:
            raise _budget_error()
        rows = self._reports(metadata)
        prices = ()
        if request.metric == "broker.flow_price_alignment" and self.prices is not None:
            prices = self.prices.read_prices(instrument_id=request.instrument_id, market_id=request.market_id,
                start=request.start, end=request.end, price_basis=request.parameters.get("price_basis", "raw"))
            if len(prices) > MAX_QUERY_REPORTS:
                raise _budget_error()
        return BrokerAnalysisHistory(reports=rows, calendar=calendar, prices=tuple(prices), point_in_time_verified=False)

    @staticmethod
    def _run(run):
        if run is None:
            return None
        # Never expose SQL, paths, DSNs or arbitrary source error text/stat keys.
        known = set(IMPORT_COUNTERS) | {
            "received", "accepted", "partial", "rejected", "unresolved", "skipped",
            "raw_archived", "canonical_changed",
        }
        counts = {key: value for key, value in run.stats.items()
                  if key in known and type(value) is int and 0 <= value <= 2**63 - 1}
        failure = "ingestion_failed" if run.status == "failed" else None
        return dict(id=run.id, status=run.status, started_at=run.started_at, finished_at=run.finished_at,
                    counts=counts, safe_failure_code=failure)

    def status(self, *, limit=100, source=None):
        # One extra row is allowed for the Application's 100-source lookahead.
        if type(limit) is not int or not 1 <= limit <= 101:
            raise _budget_error()
        selection = select(AUDITS.c.source_id).distinct()
        if source is not None:
            selection = selection.where(AUDITS.c.source_id == source)
        ids = self._execute(selection.order_by(AUDITS.c.source_id).limit(limit)).scalars().all()
        if not ids:
            return ()
        canonical = {row["source_id"]: dict(row) for row in self._execute(select(CANONICAL.c.source_id,
            func.min(CANONICAL.c.trade_date).label("first"), func.max(CANONICAL.c.trade_date).label("last"),
            func.count().label("count"), func.max(REVISIONS.c.source_published_at).label("published"))
            .join(REVISIONS, REVISIONS.c.id == CANONICAL.c.revision_id).where(CANONICAL.c.source_id.in_(ids))
            .group_by(CANONICAL.c.source_id)).mappings()}
        revisions = dict(self._execute(select(REVISIONS.c.source_id, func.count()).where(
            REVISIONS.c.source_id.in_(ids)).group_by(REVISIONS.c.source_id)).all())
        raw = {row["source_id"]: dict(row) for row in self._execute(select(RAW.c.source_id,
            func.count().label("count"), func.max(RAW.c.fetched_at).label("seen"))
            .where(RAW.c.source_id.in_(ids)).group_by(RAW.c.source_id)).mappings()}
        audit_counts = {}
        for key, disposition, count in self._execute(select(ITEMS.c.source_id, ITEMS.c.disposition, func.count())
                .where(ITEMS.c.source_id.in_(ids)).group_by(ITEMS.c.source_id, ITEMS.c.disposition)):
            audit_counts.setdefault(key, {})[disposition] = count
        latest = select(AUDITS.c.source_id, AUDITS.c.run_id,
            func.row_number().over(partition_by=AUDITS.c.source_id,
                order_by=(AUDITS.c.created_at.desc(), AUDITS.c.run_id.desc())).label("rank")).where(
                AUDITS.c.source_id.in_(ids)).subquery()
        run_ids = dict(self._execute(select(latest.c.source_id, latest.c.run_id).where(latest.c.rank == 1)).all())
        result = []
        for identifier in ids:
            stats, archive = canonical.get(identifier, {}), raw.get(identifier, {})
            count = stats.get("count", 0)
            result.append(dict(source=self.source(identifier), canonical_count=count,
                revision_count=revisions.get(identifier, 0), raw_count=archive.get("count", 0),
                audit_counts=audit_counts.get(identifier, {}), latest_canonical_date=stats.get("last"),
                latest_source_published_at=stats.get("published"), last_seen_at=archive.get("seen"),
                latest_run=self._run(self._runtime_run(run_ids[identifier])),
                coverage=dict(stored_from=stats.get("first"), stored_to=stats.get("last"),
                    stored_report_count=count, completeness="not_verified", expected_dates_known=False,
                    known_missing_dates=[], truncated=False),
                capabilities=dict(automated_ingestion_enabled=False, public_display_verified=False,
                    derived_display_verified=False, license_status="blocked")))
        return tuple(result)
