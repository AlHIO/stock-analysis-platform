"""Manual ownership CLI composition; no scheduling, schema or network on import."""
from dataclasses import asdict
from hashlib import sha256
import json

from stockapp.modules.ownership.application.ingestion import OwnershipIngestionService
from stockapp.modules.ownership.domain.ingestion import SourceSpec,json_value
from stockapp.modules.ownership.domain.models import OwnershipError
from .identity import load_identity_evidence
from .ingestion_store import SqlAlchemyOwnershipIngestionStore
from .providers import TdccOwnershipAdapter
from .source_manifest import TDCC_MANIFEST
from .transport import HostRateLimiter,OwnershipHttpTransport


def source_spec():
    config=json_value(asdict(TDCC_MANIFEST))
    digest=sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return SourceSpec(source_code=TDCC_MANIFEST.code,source_name=TDCC_MANIFEST.name,
        endpoint=TDCC_MANIFEST.endpoint,manifest_version=TDCC_MANIFEST.version,
        manifest_fingerprint=digest,config=config,
        approved=TDCC_MANIFEST.authorization=='ogdl_dataset_documented' and TDCC_MANIFEST.license=='OGDL-1.0')


class RuntimeProvider:
    def __init__(self,settings):
        self.settings=settings

    def parse(self,raw):
        return TdccOwnershipAdapter().parse(raw)

    async def fetch(self, *,run_id,source_id,on_response):
        async with OwnershipHttpTransport(max_attempts=min(3,self.settings.ingestion_max_attempts),
            max_response_bytes=min(20_000_000,self.settings.ingestion_max_response_bytes),
            limiter=HostRateLimiter(max(5.0,self.settings.ingestion_min_interval_seconds))) as transport:
            async with TdccOwnershipAdapter(network_enabled=True,transport=transport) as adapter:
                return await adapter.fetch(run_id=run_id,source_id=source_id,on_response=on_response)


async def execute(engine,args,settings):
    store=SqlAlchemyOwnershipIngestionStore(engine,
        host_interval=max(5.0,settings.ingestion_min_interval_seconds))
    if args.ownership_action=='status':
        return store.inspect(args.id,limit=args.limit)
    service=OwnershipIngestionService(store,source_spec(),RuntimeProvider(settings))
    if args.ownership_action=='sync':
        return [await service.sync(evidence=load_identity_evidence(args.identity_evidence))]
    if args.resume is not None:
        return [await service.resume(args.resume)]
    return [await service.replay(args.raw,evidence=load_identity_evidence(args.identity_evidence)
        if args.identity_evidence else None)]


def exit_code(args,reports):
    if args.ownership_action=='status':
        return 0 if reports or args.id is None else 2
    return 0 if reports and all(report['status']=='succeeded' for report in reports) else 2
