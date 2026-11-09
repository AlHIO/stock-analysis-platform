"""CLI composition: explicit approved downloads, durable replay, read-only status."""
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.margin.application.ingestion import MarginIngestionService
from stockapp.modules.margin.application.normalization import MarginNormalizer
from stockapp.modules.margin.domain.models import MarginError
from stockapp.modules.margin.domain.persistence import SourceSpec, json_value
from .identity import (
    ReviewedMarginResolver, date_timing_from_evidence, load_margin_evidence,
)
from .ingestion_store import SqlAlchemyMarginIngestionStore
from .providers import TpexMarginAdapter, TwseMarginAdapter, TwseDatedMarginAdapter
from .source_manifest import MANIFESTS, TPEX_MANIFEST, TWSE_DATED_MANIFEST, TWSE_OPENAPI_MANIFEST
from .transport import HostRateLimiter, MarginHttpTransport

ADAPTERS = {
    TPEX_MANIFEST.contract.key:TpexMarginAdapter,
    TWSE_OPENAPI_MANIFEST.contract.key:TwseMarginAdapter,
    TWSE_DATED_MANIFEST.contract.key:TwseDatedMarginAdapter,
}


def source_spec(manifest):
    return SourceSpec(source_code=manifest.contract.key,
        source_name=f'{manifest.provider}: official margin balance',
        manifest_version=manifest.version, config=json_value(asdict(manifest)))


class RuntimeProvider:
    def __init__(self, settings, manifest):
        self.settings, self.manifest = settings, manifest

    def parse(self, raw, *, date_evidence=None):
        return ADAPTERS[self.manifest.contract.key]().parse(raw, date_evidence=date_evidence)

    async def fetch(self, *, run_id, source_id, trade_date=None, on_response=None):
        transport = MarginHttpTransport(
            max_attempts=min(3, self.settings.ingestion_max_attempts),
            max_response_bytes=min(20_000_000, self.settings.ingestion_max_response_bytes),
            limiter=HostRateLimiter(max(5.0, self.settings.ingestion_min_interval_seconds)))
        try:
            async with ADAPTERS[self.manifest.contract.key](
                network_enabled=True, transport=transport) as adapter:
                return await adapter.fetch(run_id=run_id, source_id=source_id,
                    trade_date=trade_date, on_response=on_response)
        finally:
            await transport.aclose()


class _FrozenIdentityReader:
    def __init__(self, values):
        self.values = values

    def find_by_symbol(self, symbol):
        return self.values.get(symbol, ())


def normalizer_factory(engine, manifest):
    def create(saved_evidence, raw):
        if len(saved_evidence) > 1:
            raise MarginError('ambiguous_margin_evidence_document')
        evidence = saved_evidence[0] if saved_evidence else load_margin_evidence(None)
        # Freeze only reviewed symbols; absent evidence cannot establish historic
        # identity, so no per-row all-market lookup or private table access.
        with Session(engine) as session, session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            reader = instrument_identity_reader(session)
            identities = {entry['symbol']:reader.find_by_symbol(entry['symbol'])
                          for entry in evidence['entries']}
        resolver = ReviewedMarginResolver(_FrozenIdentityReader(identities), evidence)
        date_evidence = date_timing_from_evidence(raw, evidence)
        return MarginNormalizer(resolver, manifest=manifest, date_evidence=date_evidence), date_evidence
    return create


def _manifest(args, store):
    if args.margin_action == 'sync':
        if args.source == 'dated':
            if args.market != 'twse':
                raise MarginError('dated_source_not_supported_for_market')
            return TWSE_DATED_MANIFEST
        return TPEX_MANIFEST if args.market == 'tpex' else TWSE_OPENAPI_MANIFEST
    if args.resume is not None:
        job = store.get(args.resume)
        if job is None:
            raise MarginError('margin_job_not_found')
        key = job.config.get('contract', {}).get('key')
        if key not in MANIFESTS:
            raise MarginError('replay_source_contract_not_supported')
        return MANIFESTS[key]
    raw = store.get_raw(args.raw)
    if raw is None:
        raise MarginError('margin_archive_not_found')
    manifests = [item for item in MANIFESTS.values()
                 if item.contract.version == raw.source_contract_version and item.endpoint == raw.endpoint]
    if len(manifests) != 1:
        raise MarginError('replay_source_contract_not_supported')
    return manifests[0]


async def execute(engine, args, settings):
    store = SqlAlchemyMarginIngestionStore(engine,
        host_interval=max(5.0, settings.ingestion_min_interval_seconds))
    if args.margin_action == 'status':
        return store.inspect(args.id, limit=args.limit)
    manifest = _manifest(args, store)
    service = MarginIngestionService(store, source_spec(manifest),
        RuntimeProvider(settings, manifest), normalizer_factory(engine, manifest))
    if args.margin_action == 'sync':
        evidence = load_margin_evidence(args.identity_evidence)
        return [await service.sync(evidence=[evidence] if args.identity_evidence else [],
                                   requested_date=args.date)]
    if args.resume is not None:
        return [await service.resume(args.resume)]
    evidence = [load_margin_evidence(args.identity_evidence)] if args.identity_evidence else None
    return [await service.replay(args.raw, evidence=evidence)]


def exit_code(args, reports):
    if args.margin_action == 'status':
        return 0 if reports or args.id is None else 2
    return 0 if reports and all(report['status'] == 'succeeded' for report in reports) else 2
