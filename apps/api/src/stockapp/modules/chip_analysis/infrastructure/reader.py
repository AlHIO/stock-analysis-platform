"""Compose public canonical readers sharing the caller's existing SQL Session."""
from datetime import date, timedelta

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.institutional.public import institutional_reader, InstitutionalHistory, InstitutionalQueryError
from stockapp.modules.ownership.public import ownership_reader, OwnershipQueryError
from stockapp.modules.margin.public import margin_reader, MarginQueryError
from stockapp.modules.lending.public import lending_reader, LendingQueryError
from stockapp.modules.chip_analysis.domain.models import Metric, RevisionObservation
from stockapp.modules.chip_analysis.domain.queries import (
    ChipQueryData, ChipQueryError, LOOKBACK_DAYS, MAX_INPUT_REPORTS, PRODUCTS,
)


class SqlAlchemyChipQueryReader:
    def __init__(self, session, *, institutional=None, ownership=None, margin=None, lending=None):
        self.references = market_data_reference_reader(session)
        self.institutional = institutional if institutional is not None else institutional_reader(session)
        self.ownership = ownership if ownership is not None else ownership_reader(session)
        self.margin = margin if margin is not None else margin_reader(session)
        self.lending = lending if lending is not None else lending_reader(session)

    def instrument(self, identifier):
        try:
            return self.references.instrument(identifier)
        except (ValueError, KeyError, TypeError) as exc:
            raise ChipQueryError('invalid_stored_chip_data', 'Chip metrics are temporarily unavailable.', 503) from exc

    @staticmethod
    def _bounded(reports):
        if len(reports) > MAX_INPUT_REPORTS:
            raise ChipQueryError('query_input_limit_exceeded', 'Narrow the date range or source selection.')
        return tuple(reports)

    def read(self, instrument, *, metrics, start, end, selections):
        try:
            return self._read(instrument, metrics=metrics, start=start, end=end, selections=selections)
        except (InstitutionalQueryError, OwnershipQueryError, MarginQueryError, LendingQueryError) as exc:
            raise ChipQueryError('invalid_stored_chip_data', 'Chip metrics are temporarily unavailable.', 503) from exc

    def _read(self, instrument, *, metrics, start, end, selections):
        identifier, market_id = instrument['id'], instrument['market']['id']
        products = tuple(dict.fromkeys(PRODUCTS[metric] for metric in metrics))
        history, weeks, schedule, margins, loans = None, (), None, (), ()
        observations, sources, availability = [], {}, []
        start_warm = max(date.min, start - timedelta(days=min(LOOKBACK_DAYS, start.toordinal()-1)))

        def record(product, reader, reports, *, extra=()):
            for report in reports:
                source_id = report.lineage.source_id
                if source_id not in sources:
                    sources[source_id] = reader.source(source_id)
            dates = [r.as_of_date if product == 'ownership_weekly' else r.trade_date for r in reports]
            timings = [r if product == 'ownership_weekly' else r.timing for r in reports]
            known_ingested = []
            for report, timing in zip(reports, timings):
                if product in ('margin_daily', 'lending_daily'):
                    item = reader.observation_timing(report)
                    observation = RevisionObservation(revision_id=report.lineage.revision_id,
                        ingested_at=item['ingested_at'], last_seen_at=item['last_seen_at'])
                    observations.append(observation)
                    known_ingested.append(observation.ingested_at)
                else:
                    known_ingested.append(timing.ingested_at)
            availability.append(dict(product=product, frequency='1w' if product == 'ownership_weekly' else '1d',
                loaded_reports=len(reports), stored_from=min(dates, default=None), stored_to=max(dates, default=None),
                latest_source_published_at=max((t.source_published_at for t in timings if t.source_published_at), default=None),
                last_seen_at=max((getattr(t, 'last_seen_at', t.first_seen_at) for t in timings), default=None),
                ingested_at=max(known_ingested, default=None),
                source_ids=sorted({r.lineage.source_id for r in reports}, key=str),
                state='not_verified' if reports else 'no_data',
                reasons=list(extra) if reports else ['no_canonical_reports_in_query_snapshot']))

        if 'institutional_daily' in products:
            scopes = self.institutional.scopes(identifier)
            if selections.institutional_scope is not None:
                scopes = tuple(v for v in scopes if v.code == selections.institutional_scope)
            if len(scopes) > 1:
                raise ChipQueryError('ambiguous_trade_scope', 'Select an institutional trade scope.')
            scope = scopes[0] if scopes else None
            reports = self._bounded(self.institutional.page(identifier, scope, start_warm, end,
                after=None, order='asc', limit=MAX_INPUT_REPORTS+1)) if scope else ()
            calendar = self.institutional.calendar(market_id, start_warm, end, tuple(r.trade_date for r in reports))
            # An observed boundary is never a verified start of all history.
            history = InstitutionalHistory(instrument_id=identifier, market_id=market_id,
                calendar=calendar, reports=reports, history_start_verified=False) if reports else None
            record('institutional_daily', self.institutional, reports, extra=('calendar_coverage_not_verified',) if not calendar.verified else ())
        if 'ownership_weekly' in products:
            # Bounded reverse index read also supplies older available snapshots
            # for as-of alignment; do not equate 'last two observed' with weeks.
            weeks = tuple(reversed(self._bounded(self.ownership.page(identifier, date.min, end,
                after=None, order='desc', limit=MAX_INPUT_REPORTS+1))))
            schedule_start = weeks[0].as_of_date if weeks else start
            schedule = self.ownership.schedule(schedule_start, end, tuple(r.as_of_date for r in weeks))
            record('ownership_weekly', self.ownership, weeks, extra=('weekly_schedule_not_verified',) if not schedule.verified else ())
        if 'margin_daily' in products:
            margins = self._bounded(self.margin.page(identifier, start, end, scope=selections.margin_scope,
                after=None, order='asc', limit=MAX_INPUT_REPORTS+1))
            record('margin_daily', self.margin, margins)
        if 'lending_daily' in products:
            loans = self._bounded(self.lending.page(identifier, start, end,
                report_kind=selections.report_kind, scope=selections.lending_scope,
                channel=selections.lending_channel, family=selections.channel_family,
                after=None, order='asc', limit=MAX_INPUT_REPORTS+1))
            channels = {(r.channel.family, r.channel.code, r.channel.version) for r in loans}
            if len(channels) > 1:
                raise ChipQueryError('ambiguous_lending_channel', 'Select one lending channel and family.')
            record('lending_daily', self.lending, loans)
        if Metric.LENDING_SHORT_SALE_RATIO in metrics:
            # No reviewed public traded-volume/scope mapping is deployed yet.
            # DailyBar.volume_shares alone is not comparable-scope evidence.
            availability.append(dict(product='market_traded_quantity', frequency='1d', loaded_reports=0,
                stored_from=None, stored_to=None, latest_source_published_at=None,
                last_seen_at=None, ingested_at=None, source_ids=[], state='no_data',
                reasons=['reviewed_ratio_denominator_mapping_unavailable']))
        return ChipQueryData(institutional=history, ownership=weeks, weekly_schedule=schedule,
            margin=margins, lending=loans, observations=tuple(observations),
            sources=tuple(sources.values()), availability=tuple(availability))
