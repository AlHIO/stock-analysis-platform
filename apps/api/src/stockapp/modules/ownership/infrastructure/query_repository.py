"""Bounded, canonical-only ownership reads in the caller's transaction snapshot.

Report pages batch their typed buckets and lineage; they never parse raw payloads
or change promotion decisions. Reference metadata stays behind market_data.public.
"""
from collections import defaultdict
import re

from sqlalchemy import case, func, select

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.ownership.domain.models import (
    AdjustmentConvention, BucketDefinition, BucketKind, BucketScheme, HoldingBucket,
    OwnershipSourceLineage, OwnershipWeeklyReport,
)
from stockapp.modules.ownership.domain.queries import OwnershipQueryError
from stockapp.modules.ownership.domain.query_results import WeeklyScheduleSnapshot
from .repositories import CANONICAL, REVISIONS, BUCKETS, ITEMS, SCHEMES, DEFINITIONS, RAW, JOBS
from .source_manifest import TDCC_MANIFEST

_OBSERVATION_FIELDS = ('id','run_id','raw_id','observed_at','ingested_at','source_published_on',
    'source_published_at','source_row_locator','parser_version','identity_evidence_version','promotion_policy_version')


def _stored_error():
    return OwnershipQueryError('invalid_stored_ownership_data', 'Stored ownership data is temporarily unavailable.', 503)


class SqlAlchemyOwnershipQueryRepository:
    def __init__(self, session, *, schedule_reader=None):
        self.session = session
        self.references = market_data_reference_reader(session)
        self.schedule_reader = schedule_reader
        self._schemes = {}
        self._sources = {}

    def _execute(self, statement):
        return self.session.execute(statement.execution_options(autoflush=False, populate_existing=True))

    def instrument(self, instrument_id):
        try:
            return self.references.instrument(instrument_id)
        except (ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc

    def source(self, source_id):
        if source_id not in self._sources:
            value = self.references.source(source_id)
            if value is None:
                raise _stored_error()
            self._sources[source_id] = dict(value)
        return dict(self._sources[source_id])

    @staticmethod
    def _select():
        return select(*REVISIONS.c, *(ITEMS.c[name].label('observation_' + name) for name in _OBSERVATION_FIELDS))\
            .select_from(CANONICAL).join(REVISIONS, REVISIONS.c.id == CANONICAL.c.revision_id)\
            .join(ITEMS, ITEMS.c.id == CANONICAL.c.observation_id)

    def _load_schemes(self, versions):
        missing = set(versions) - self._schemes.keys()
        if not missing:
            return
        headers = self._execute(select(SCHEMES).where(SCHEMES.c.version.in_(missing))).mappings().all()
        ordering = case((DEFINITIONS.c.kind == 'regular', 0), (DEFINITIONS.c.kind == 'adjustment', 1), else_=2)
        definitions = defaultdict(list)
        for row in self._execute(select(DEFINITIONS).where(DEFINITIONS.c.scheme_version.in_(missing))
                .order_by(DEFINITIONS.c.scheme_version, ordering, DEFINITIONS.c.lower_inclusive, DEFINITIONS.c.bucket_code)).mappings():
            definitions[row['scheme_version']].append(BucketDefinition(code=row['bucket_code'], kind=BucketKind(row['kind']),
                label=row['label'], lower_inclusive=row['lower_inclusive'], upper_inclusive=row['upper_inclusive']))
        for header in headers:
            values = definitions[header['version']]
            if len(values) != header['expected_bucket_count']:
                raise _stored_error()
            self._schemes[header['version']] = BucketScheme(version=header['version'], definitions=tuple(values),
                source_contract_version=header['source_contract_version'],
                adjustment_convention=AdjustmentConvention(header['adjustment_convention']))
        if not missing <= self._schemes.keys():
            raise _stored_error()

    def _reports(self, rows):
        if not rows:
            return ()
        try:
            self._load_schemes(row['scheme_version'] for row in rows)
            identifiers = tuple({row['id'] for row in rows})
            buckets = defaultdict(list)
            for bucket in self._execute(select(BUCKETS).where(BUCKETS.c.revision_id.in_(identifiers))).mappings():
                buckets[bucket['revision_id']].append(HoldingBucket(code=bucket['bucket_code'], kind=BucketKind(bucket['kind']),
                    holder_count=bucket['holder_count'], quantity=bucket['quantity'], reported_percent=bucket['reported_percent']))
            first_seen = dict(self._execute(select(ITEMS.c.revision_id, func.min(ITEMS.c.observed_at)).where(
                ITEMS.c.revision_id.in_(identifiers), ITEMS.c.disposition == 'accepted').group_by(ITEMS.c.revision_id)).all())
            result = []
            for row in rows:
                scheme = self._schemes[row['scheme_version']]
                order = {code:index for index,code in enumerate(scheme.codes)}
                report = OwnershipWeeklyReport(instrument_id=row['instrument_id'], market_id=row['market_id'],
                    as_of_date=row['as_of_date'], quantity_unit=row['quantity_unit'], denominator=row['denominator'], scheme=scheme,
                    buckets=tuple(sorted(buckets[row['id']], key=lambda bucket:order.get(bucket.code, len(order)))),
                    lineage=OwnershipSourceLineage(source_id=row['source_id'], run_id=row['observation_run_id'],
                        raw_id=row['observation_raw_id'], revision_id=row['id'], source_contract_version=row['source_contract_version'],
                        parser_version=row['observation_parser_version'], source_row_locator=row['observation_source_row_locator'],
                        identity_evidence_version=row['observation_identity_evidence_version'],
                        promotion_policy_version=row['observation_promotion_policy_version'], payload_hash=row['payload_hash']),
                    first_seen_at=first_seen[row['id']], last_seen_at=row['observation_observed_at'],
                    ingested_at=row['observation_ingested_at'], source_published_on=row['observation_source_published_on'],
                    source_published_at=row['observation_source_published_at'])
                if not row['is_sealed'] or report.completeness.value != row['completeness']:
                    raise _stored_error()
                result.append(report)
            return tuple(result)
        except (ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc

    def page(self, instrument_id, start, end, *, after=None, order='asc', limit=250):
        statement = self._select().where(CANONICAL.c.instrument_id == instrument_id,
            CANONICAL.c.as_of_date >= start, CANONICAL.c.as_of_date <= end)
        if after is not None:
            statement = statement.where(CANONICAL.c.as_of_date > after if order == 'asc' else CANONICAL.c.as_of_date < after)
        ordering = CANONICAL.c.as_of_date.asc() if order == 'asc' else CANONICAL.c.as_of_date.desc()
        rows = self._execute(statement.order_by(ordering).limit(limit)).mappings().all()
        return self._reports(rows)

    def latest(self, instrument_id):
        row = self._execute(self._select().where(CANONICAL.c.instrument_id == instrument_id)
            .order_by(CANONICAL.c.as_of_date.desc()).limit(1)).mappings().one_or_none()
        return self._reports([row])[0] if row is not None else None

    def previous(self, instrument_id, before):
        row = self._execute(self._select().where(CANONICAL.c.instrument_id == instrument_id,
            CANONICAL.c.as_of_date < before).order_by(CANONICAL.c.as_of_date.desc()).limit(1)).mappings().one_or_none()
        return self._reports([row])[0] if row is not None else None

    def extent(self, instrument_id):
        return tuple(self._execute(select(func.min(CANONICAL.c.as_of_date), func.max(CANONICAL.c.as_of_date), func.count())
            .where(CANONICAL.c.instrument_id == instrument_id)).one())

    def schedule(self, start, end, observed_dates=()):
        if self.schedule_reader is not None:
            try:
                value = self.schedule_reader.load(start=start, end=end)
                if not isinstance(value, WeeklyScheduleSnapshot):
                    raise _stored_error()
                return value
            except (ValueError, KeyError, TypeError) as exc:
                raise _stored_error() from exc
        # A saved observation is not proof of all expected statistics dates.
        return WeeklyScheduleSnapshot(start=start, end=end,
            as_of_dates=tuple(sorted({day for day in observed_dates if start <= day <= end})),
            verified=False, version='ownership-observed-unverified-v1')

    @staticmethod
    def _run(run):
        if run is None:
            return None
        counts = dict(run.stats)
        partial = counts.get('accepted', 0) > 0 and (run.status == 'failed' or any(
            counts.get(key, 0) > 0 for key in ('partial','unresolved','rejected','candidate','conflict')))
        code = None
        if run.status == 'failed':
            code = run.error_summary if run.error_summary and re.fullmatch(r'[a-z0-9_]{1,100}', run.error_summary) else 'ingestion_failed'
        return dict(id=run.id, status=run.status, started_at=run.started_at, finished_at=run.finished_at,
            has_partial_data=partial, safe_failure_code=code, counts=counts)

    def status(self):
        try:
            source = self.references.source_by_code(TDCC_MANIFEST.code)
            summary = dict(first=None, last=None, count=0, published=None, observed=None)
            reported = None
            raw_count = candidate_count = 0
            audit_counts = dict.fromkeys(('accepted','rejected','unresolved','skipped'), 0)
            latest_run = last_success = None
            if source is not None:
                source_id = source['id']
                values = self._execute(select(func.min(CANONICAL.c.as_of_date), func.max(CANONICAL.c.as_of_date),
                    func.count(), func.max(ITEMS.c.source_published_at), func.max(CANONICAL.c.observed_at))
                    .select_from(CANONICAL).join(ITEMS, ITEMS.c.id == CANONICAL.c.observation_id)
                    .where(CANONICAL.c.source_id == source_id)).one()
                summary = dict(zip(summary, values, strict=True))
                reported = self._execute(select(func.max(JOBS.c.as_of_date)).where(JOBS.c.source_id == source_id)).scalar_one()
                item_date, item_published = self._execute(select(func.max(ITEMS.c.as_of_date), func.max(ITEMS.c.source_published_at))
                    .where(ITEMS.c.source_id == source_id, ITEMS.c.disposition == 'accepted')).one()
                reported = max((day for day in (reported, item_date, summary['last']) if day is not None), default=None)
                summary['published'] = max((value for value in (summary['published'], item_published) if value is not None), default=None)
                raw_count, raw_observed = self._execute(select(func.count(), func.max(RAW.c.fetched_at))
                    .where(RAW.c.source_id == source_id)).one()
                summary['observed'] = max((value for value in (summary['observed'], raw_observed) if value is not None), default=None)
                audit_counts.update(dict(self._execute(select(ITEMS.c.disposition, func.count()).where(
                    ITEMS.c.source_id == source_id).group_by(ITEMS.c.disposition)).all()))
                candidate_count = self._execute(select(func.count(func.distinct(ITEMS.c.revision_id))).where(
                    ITEMS.c.source_id == source_id, ITEMS.c.disposition == 'accepted', ITEMS.c.promotion_decision == 'candidate',
                    ITEMS.c.revision_id.not_in(select(CANONICAL.c.revision_id)))).scalar_one()
                latest_run = self._run(self.references.latest_run(source_id))
                last_success = self._run(self.references.latest_run(source_id, status='succeeded'))
            approved = TDCC_MANIFEST.authorization == 'ogdl_dataset_documented'
            return dict(key='TDCC:' + TDCC_MANIFEST.code, product='ownership_weekly', frequency='1w',
                source=source or dict(id=None, code=TDCC_MANIFEST.code, name=TDCC_MANIFEST.name),
                capabilities=dict(latest=approved, date_query=False, archive=approved, manual=approved,
                    license_status='verified_open' if approved else 'unresolved', license=TDCC_MANIFEST.license,
                    automated_ingestion_enabled=False, limitations=list(TDCC_MANIFEST.limitations)),
                latest_run=latest_run, last_successful_run=last_success, latest_reported_as_of_date=reported,
                latest_canonical_date=summary['last'], latest_source_published_at=summary['published'], last_seen_at=summary['observed'],
                canonical_count=summary['count'], candidate_count=candidate_count, raw_count=raw_count, audit_counts=audit_counts,
                coverage=dict(stored_from=summary['first'], stored_to=summary['last'], completeness='not_verified',
                    declared_scope='canonical_only', expected_dates_known=False, known_missing_dates=[], truncated=False))
        except (ValueError, KeyError, TypeError) as exc:
            raise _stored_error() from exc
