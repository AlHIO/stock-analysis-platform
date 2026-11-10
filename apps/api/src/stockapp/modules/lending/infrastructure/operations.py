"""Manual lending CLI composition; no source I/O occurs during import/status."""
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.lending.application.ingestion import LendingIngestionService
from stockapp.modules.lending.application.normalization import LendingNormalizer
from stockapp.modules.lending.domain.models import LendingError
from stockapp.modules.lending.domain.persistence import SourceSpec,json_value
from .identity import (
    ReviewedLendingResolver,date_timing_from_evidence,load_lending_evidence,
)
from .ingestion_store import SqlAlchemyLendingIngestionStore
from .providers import (
    TpexLendingAdapter,TwseBorrowedAdapter,TwseLendingShortAdapter,
    TpexDatedLendingAdapter,TpexLendingFlowAdapter,TwseQuotaAdapter,
)
from .source_manifest import MANIFESTS
from .transport import HostRateLimiter,LendingHttpTransport

ADAPTERS={
    'M3':TwseLendingShortAdapter,'M4':TwseBorrowedAdapter,'M6':TpexLendingAdapter,
    'M7':TpexDatedLendingAdapter,'M8':TpexLendingFlowAdapter,'M9':TwseQuotaAdapter,
}


def source_spec(manifest):
    return SourceSpec(source_code=manifest.contract.key,source_name=manifest.attribution,
        manifest_version=manifest.version,config=json_value(asdict(manifest)))


class RuntimeProvider:
    def __init__(self,settings,manifest):
        self.settings,self.manifest=settings,manifest

    def parse(self,raw, *,date_evidence=None):
        return ADAPTERS[self.manifest.audit_id]().parse(raw,date_evidence=date_evidence)

    async def fetch(self, *,run_id,source_id,trade_date=None,on_response=None):
        transport=LendingHttpTransport(
            max_attempts=min(3,self.settings.ingestion_max_attempts),
            max_response_bytes=min(20_000_000,self.settings.ingestion_max_response_bytes),
            limiter=HostRateLimiter(max(5.0,self.settings.ingestion_min_interval_seconds)))
        try:
            async with ADAPTERS[self.manifest.audit_id](network_enabled=True,transport=transport) as adapter:
                return await adapter.fetch(run_id=run_id,source_id=source_id,
                    trade_date=trade_date,on_response=on_response)
        finally:
            await transport.aclose()


class _FrozenIdentityReader:
    def __init__(self,values):
        self.values=values

    def find_by_symbol(self,symbol):
        return self.values.get(symbol,())


def normalizer_factory(engine,manifest):
    def create(saved_evidence,raw):
        if len(saved_evidence)>1:
            raise LendingError('ambiguous_lending_evidence_document')
        evidence=saved_evidence[0] if saved_evidence else load_lending_evidence(None)
        with Session(engine) as session,session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            reader=instrument_identity_reader(session)
            identities={entry['symbol']:reader.find_by_symbol(entry['symbol'])
                        for entry in evidence['entries']}
        resolver=ReviewedLendingResolver(_FrozenIdentityReader(identities),evidence)
        proof=date_timing_from_evidence(raw,evidence)
        return LendingNormalizer(resolver,manifest=manifest,date_evidence=proof,scope='unspecified'),proof
    return create


def _manifest(args,store):
    if args.lending_action=='sync':
        return MANIFESTS[args.source]
    if args.resume is not None:
        job=store.get(args.resume)
        if job is None:
            raise LendingError('lending_job_not_found')
        key=job.config.get('contract',{}).get('key')
        choices=[value for value in MANIFESTS.values() if value.contract.key==key]
    else:
        raw=store.get_raw(args.raw)
        if raw is None:
            raise LendingError('lending_archive_not_found')
        choices=[value for value in MANIFESTS.values()
                 if (value.endpoint,value.contract.version)==(raw.endpoint,raw.source_contract_version)]
    if len(choices)!=1:
        raise LendingError('replay_source_contract_not_supported')
    return choices[0]


async def execute(engine,args,settings):
    store=SqlAlchemyLendingIngestionStore(engine,
        host_interval=max(5.0,settings.ingestion_min_interval_seconds))
    if args.lending_action=='status':
        return store.inspect(args.id,limit=args.limit)
    manifest=_manifest(args,store)
    service=LendingIngestionService(store,source_spec(manifest),
        RuntimeProvider(settings,manifest),normalizer_factory(engine,manifest))
    if args.lending_action=='sync':
        evidence=[load_lending_evidence(args.identity_evidence)] if args.identity_evidence else []
        return [await service.sync(evidence=evidence,requested_date=args.date)]
    if args.resume is not None:
        return [await service.resume(args.resume)]
    evidence=[load_lending_evidence(args.identity_evidence)] if args.identity_evidence else None
    return [await service.replay(args.raw,evidence=evidence)]


def exit_code(args,reports):
    if args.lending_action=='status':
        return 0 if reports or args.id is None else 2
    return 0 if reports and all(value['status']=='succeeded' for value in reports) else 2
