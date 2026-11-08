"""Manual institutional CLI composition; no scheduling or implicit migration."""
from dataclasses import asdict
from hashlib import sha256
import json

from stockapp.modules.institutional.application.ingestion import InstitutionalIngestionService
from stockapp.modules.institutional.domain.ingestion import SourceSpec, json_value
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from .identity import load_identity_evidence
from .ingestion_store import SqlAlchemyInstitutionalIngestionStore
from .providers import TpexInstitutionalAdapter, TwseInstitutionalAdapter
from .source_manifest import TPEX_MANIFEST, TWSE_MANIFEST
from .transport import HostRateLimiter, InstitutionalHttpTransport

MANIFESTS = {"twse": TWSE_MANIFEST, "tpex": TPEX_MANIFEST}
ADAPTERS = {"twse": TwseInstitutionalAdapter, "tpex": TpexInstitutionalAdapter}


def source_spec(provider: str) -> SourceSpec:
    if provider not in MANIFESTS:
        raise InstitutionalSourceError("institutional_source_unknown")
    manifest = MANIFESTS[provider]
    fingerprint = sha256(json.dumps(json_value(asdict(manifest)), ensure_ascii=True,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return SourceSpec(provider=provider, market_code=provider.upper(),
        market_name="臺灣證券交易所上市" if provider == "twse" else "證券櫃檯買賣中心上櫃",
        source_code="TWSE_INSTITUTIONAL_T86" if provider == "twse" else "TPEX_INSTITUTIONAL_OPENAPI",
        source_name="臺灣證券交易所：法人 T86" if provider == "twse" else "證券櫃檯買賣中心：法人 OpenAPI",
        manifest_version=manifest.version, manifest_fingerprint=fingerprint, endpoint=manifest.endpoint,
        authorization=manifest.authorization, capability=manifest.capability,
        verified_dates=tuple(sorted({day for contract in manifest.contracts for day in contract.verified_historical_dates})),
        limitations=manifest.known_limitations, license=manifest.license,
        attribution="臺灣證券交易所" if provider == "twse" else "證券櫃檯買賣中心",
        evidence_urls=manifest.evidence_urls)


class RuntimeProvider:
    """Assembly only: source adapters and their existing bounded HTTP transport."""
    def __init__(self, provider, settings):
        self.provider, self.settings = provider, settings

    def parse(self, raw, *, requested_date=None):
        return ADAPTERS[self.provider]().parse(raw, requested_date=requested_date)

    async def fetch(self, *, run_id, source_id, market_id, trade_date, on_response):
        interval = max(5.0, self.settings.ingestion_min_interval_seconds)
        async with InstitutionalHttpTransport(
            max_attempts=min(3, self.settings.ingestion_max_attempts),
            max_response_bytes=min(20_000_000, self.settings.ingestion_max_response_bytes),
            limiter=HostRateLimiter(interval), on_response=on_response,
        ) as transport:
            async with ADAPTERS[self.provider](network_enabled=True, transport=transport) as adapter:
                return await adapter.fetch(run_id=run_id, source_id=source_id,
                    market_id=market_id, trade_date=trade_date)


async def execute(engine, args, settings) -> list[dict]:
    store = SqlAlchemyInstitutionalIngestionStore(engine,
        host_interval=max(5.0, getattr(settings, "ingestion_min_interval_seconds", 5.0)))
    if args.institutional_action == "status":
        return store.inspect(args.id, limit=args.limit)
    if args.institutional_action == "sync":
        provider = args.market
    elif args.resume:
        job = store.get(args.resume)
        if job is None:
            raise InstitutionalSourceError("institutional_job_not_found")
        provider = job.provider
    else:
        raw = store.get_raw(args.raw)
        if raw is None:
            raise InstitutionalSourceError("institutional_archive_not_found")
        provider = next((key for key, manifest in MANIFESTS.items() if raw.endpoint == manifest.endpoint), None)
        if provider is None:
            raise InstitutionalSourceError("institutional_source_unknown")
    source = source_spec(provider)
    service = InstitutionalIngestionService(store, source, RuntimeProvider(provider, settings))
    if args.institutional_action == "sync":
        return [await service.sync(trade_date=args.date, evidence=load_identity_evidence(args.identity_evidence))]
    if args.resume:
        return [await service.resume(args.resume)]
    evidence = load_identity_evidence(args.identity_evidence) if args.identity_evidence else None
    return [await service.replay(args.raw, evidence=evidence)]


def exit_code(args, reports: list[dict]) -> int:
    """A successful status lookup may describe a failed or partial ingestion."""
    if args.institutional_action == "status":
        return 0 if reports or args.id is None else 2
    return 0 if reports and all(
        report.get("outcome", report.get("status")) == "succeeded" for report in reports
    ) else 2
