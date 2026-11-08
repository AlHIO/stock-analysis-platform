"""Canonical institutional projections inside the caller's read-only snapshot.

Financial reports are reconstructed by the Phase 2B-2 repository. Candidate
revisions/raw bytes never become query points; status only exposes audit counts.
"""
from datetime import date
import re
from uuid import UUID

from sqlalchemy import func, select

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.institutional.domain.models import (
    CalendarSnapshot, InstitutionalValidationError, QuantityUnit, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import PromotionPolicy, scope_values
from stockapp.modules.institutional.domain.queries import InstitutionalQueryError
from .ingestion_models import InstitutionalIngestionJobRecord
from .repositories import CANONICAL, ITEMS, SqlAlchemyInstitutionalRepository
from .source_manifest import TPEX_MANIFEST, TWSE_MANIFEST

JOBS = InstitutionalIngestionJobRecord.__table__
_CATALOG = (
    ("TPEX", "TPEX_INSTITUTIONAL_OPENAPI", "證券櫃檯買賣中心上櫃", "證券櫃檯買賣中心：法人 OpenAPI", TPEX_MANIFEST),
    ("TWSE", "TWSE_INSTITUTIONAL_T86", "臺灣證券交易所上市", "臺灣證券交易所：法人 T86", TWSE_MANIFEST),
)


def _stored_error():
    return InstitutionalQueryError("invalid_stored_chip_data", "Stored institutional data is temporarily unavailable.", 503)


class SqlAlchemyInstitutionalQueryRepository:
    def __init__(self, session, *, calendar_reader=None):
        self.session = session
        self.references = market_data_reference_reader(session)
        self.calendar_reader = calendar_reader

    def _execute(self, statement):
        return self.session.execute(statement.execution_options(autoflush=False, populate_existing=True))

    def instrument(self, instrument_id: UUID):
        try:
            return self.references.instrument(instrument_id)
        except (InstitutionalValidationError, ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc

    def source(self, source_id: UUID):
        value = self.references.source(source_id)
        if value is None:
            raise _stored_error()
        return value

    def scopes(self, instrument_id: UUID):
        values = self._execute(select(CANONICAL.c.scope).where(
            CANONICAL.c.instrument_id == instrument_id).distinct()).scalars().all()
        try:
            return tuple(TradeScope(**(dict(value) | {"basis": TradeBasis(value["basis"])})) for value in values)
        except (InstitutionalValidationError, ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc

    @staticmethod
    def _where(instrument_id, scope):
        return (CANONICAL.c.instrument_id == instrument_id, CANONICAL.c.scope == scope_values(scope))

    def _report(self, row):
        try:
            policy = PromotionPolicy(source_id=row["source_id"],
                source_contract_version=row["source_contract_version"], quantity_unit=QuantityUnit(row["quantity_unit"]))
            scope = TradeScope(**(dict(row["scope"]) | {"basis": TradeBasis(row["scope"]["basis"])}))
            with self.session.no_autoflush:
                report = SqlAlchemyInstitutionalRepository(self.session, policy).get_canonical(
                    row["instrument_id"], row["trade_date"], scope)
            if report is None:
                raise _stored_error()
            return report
        except (InstitutionalValidationError, ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc

    def page(self, instrument_id, scope, start, end, *, after=None, order="asc", limit=250):
        if scope is None:
            return ()
        statement = select(CANONICAL).where(*self._where(instrument_id, scope),
            CANONICAL.c.trade_date >= start, CANONICAL.c.trade_date <= end)
        if after is not None:
            statement = statement.where(CANONICAL.c.trade_date > after if order == "asc" else CANONICAL.c.trade_date < after)
        ordering = CANONICAL.c.trade_date.asc() if order == "asc" else CANONICAL.c.trade_date.desc()
        rows = self._execute(statement.order_by(ordering).limit(limit)).mappings().all()
        return tuple(self._report(row) for row in rows)

    def latest(self, instrument_id, scope):
        if scope is None:
            return None
        row = self._execute(select(CANONICAL).where(*self._where(instrument_id, scope))
            .order_by(CANONICAL.c.trade_date.desc()).limit(1)).mappings().one_or_none()
        return self._report(row) if row is not None else None

    def extent(self, instrument_id, scope):
        if scope is None:
            return None, None
        return tuple(self._execute(select(func.min(CANONICAL.c.trade_date), func.max(CANONICAL.c.trade_date))
            .where(*self._where(instrument_id, scope))).one())

    def calendar(self, market_id, start, end, observed_dates=()):
        if self.calendar_reader is not None:
            snapshot = self.calendar_reader.load(market_id, start=start, end=end)
            if not isinstance(snapshot, CalendarSnapshot) or snapshot.market_id != market_id:
                raise _stored_error()
            return snapshot
        # Phase 1 calendar rows do not declare verified coverage/provenance.
        # These dates are observations only; Calculator will return not_verified.
        return CalendarSnapshot(market_id=market_id, start=start, end=end,
            session_dates=tuple(sorted(set(day for day in observed_dates if start <= day <= end))),
            verified=False, version="unverified-canonical-observations-v1")

    @staticmethod
    def _run(run):
        if run is None:
            return None
        counts = dict(run.stats)
        has_partial = counts.get("accepted", 0) > 0 and (run.status == "failed" or any(
            counts.get(key, 0) > 0 for key in ("partial", "candidate", "rejected", "unresolved", "conflict")))
        code = None
        if run.status == "failed":
            code = run.error_summary if run.error_summary is not None and re.fullmatch(r"[a-z0-9_]{1,100}", run.error_summary) else "ingestion_failed"
        return dict(id=run.id, status=run.status, started_at=run.started_at, finished_at=run.finished_at,
            has_partial_data=has_partial, safe_failure_code=code, counts=counts)

    def _status(self, market_code, source_code, market_name, source_name, manifest):
        source = self.references.source_by_code(source_code)
        market = self.references.market_by_code(market_code)
        summary = dict(stored_from=None, stored_to=None, count=0, observed_at=None, published_at=None)
        latest_run = last_success = None
        reported_date, candidate_count = None, 0
        if source is not None and market is not None:
            source_id, market_id = source["id"], market["id"]
            statement = select(func.min(CANONICAL.c.trade_date), func.max(CANONICAL.c.trade_date),
                func.count(CANONICAL.c.id), func.max(CANONICAL.c.observed_at), func.max(ITEMS.c.source_published_at))
            rows = self._execute(statement.select_from(CANONICAL).join(ITEMS, ITEMS.c.id == CANONICAL.c.observation_id)
                .where(CANONICAL.c.source_id == source_id, CANONICAL.c.market_id == market_id)).one()
            summary = dict(zip(summary, rows, strict=True))
            reported_date = self._execute(select(func.max(JOBS.c.trade_date)).where(
                JOBS.c.source_id == source_id, JOBS.c.market_id == market_id)).scalar_one()
            item_date, item_published, seen = self._execute(select(func.max(ITEMS.c.trade_date),
                func.max(ITEMS.c.source_published_at), func.max(ITEMS.c.observed_at)).where(
                ITEMS.c.source_id == source_id, ITEMS.c.market_id == market_id,
                ITEMS.c.disposition == "accepted")).one()
            reported_date = max((day for day in (reported_date, item_date) if day is not None), default=None)
            summary["published_at"] = max((value for value in (summary["published_at"], item_published)
                if value is not None), default=None)
            summary["observed_at"] = max((value for value in (summary["observed_at"], seen) if value is not None), default=None)
            selected = select(CANONICAL.c.revision_id)
            candidate_count = self._execute(select(func.count(func.distinct(ITEMS.c.revision_id))).where(
                ITEMS.c.source_id == source_id, ITEMS.c.market_id == market_id,
                ITEMS.c.disposition == "accepted", ITEMS.c.promotion_decision == "candidate",
                ITEMS.c.revision_id.not_in(selected))).scalar_one()
            latest_run = self._run(self.references.latest_run(source_id))
            last_success = self._run(self.references.latest_run(source_id, status="succeeded"))
        approved = manifest.authorization == "ogdl_dataset_documented"
        return dict(key=market_code + ":" + source_code, product="institutional_daily", frequency="1d",
            source=source or dict(id=None, code=source_code, name=source_name),
            market=market or dict(id=None, code=market_code, name=market_name, timezone=manifest.timezone, currency="TWD"),
            capabilities=dict(latest=approved and manifest.capability == "latest_only",
                date_query=approved and manifest.capability == "audited_dates_only", archive=approved, manual=approved,
                license_status="verified_open" if approved else "unresolved", license=manifest.license,
                automated_ingestion_enabled=False, limitations=list(manifest.known_limitations)),
            latest_run=latest_run, last_successful_run=last_success,
            latest_reported_as_of_date=max((day for day in (reported_date, summary["stored_to"]) if day is not None), default=None),
            latest_canonical_date=summary["stored_to"], latest_source_published_at=summary["published_at"],
            last_seen_at=summary["observed_at"], canonical_count=summary["count"], candidate_count=candidate_count,
            coverage=dict(stored_from=summary["stored_from"], stored_to=summary["stored_to"],
                completeness="not_verified", declared_scope="canonical_only", expected_dates_known=False,
                known_missing_dates=[], truncated=False))

    def status_rows(self, *, market=None, source_code=None, after=None, limit=100):
        entries = [entry for entry in _CATALOG if (market is None or entry[0] == market)
            and (source_code is None or entry[1] == source_code)
            and (after is None or entry[0] + ":" + entry[1] > after)]
        try:
            return [self._status(*entry) for entry in entries[:limit]]
        except (InstitutionalValidationError, ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc
