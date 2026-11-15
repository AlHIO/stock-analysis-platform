"""CLI composition for lawful offline imports; never performs provider HTTP I/O."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from ..application.ingestion import BrokerIngestionService
from ..domain.ingestion import parse_import_contract
from ..domain.models import BrokerFlowError
from ..domain.persistence import RawPayload
from .ingestion_store import SqlAlchemyBrokerImportStore
from .providers import OfflineBrokerAdapter


def load_import_contract(path: Path):
    """Read only a bounded regular JSON sidecar; reject duplicate keys/NaN."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BrokerFlowError("duplicate_contract_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise BrokerFlowError("nonfinite_contract_value")

    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= 1_000_000:
            raise BrokerFlowError("invalid_contract_file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            body = handle.read(1_000_001)
        if len(body) > 1_000_000:
            raise BrokerFlowError("contract_file_too_large")
        value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=invalid_constant)
        return parse_import_contract(value)
    except BrokerFlowError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise BrokerFlowError("invalid_contract_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _adapter(config):
    if (set(config) != {"contract", "filename", "encoding"}
            or not isinstance(config["filename"], str)
            or Path(config["filename"]).name != config["filename"]
            or config["encoding"] not in {"utf-8", "big5"}):
        raise BrokerFlowError("invalid_import_file_context")
    return OfflineBrokerAdapter(filename=config["filename"], encoding=config["encoding"])


async def execute(engine, args, settings):
    store = SqlAlchemyBrokerImportStore(engine)
    if args.broker_flow_action == "status":
        return store.inspect(args.id, limit=args.limit)
    if args.broker_flow_action == "import":
        contract = load_import_contract(args.contract)
        config = {"contract": contract.to_dict(), "filename": args.file.name,
                  "encoding": args.encoding}
        service = BrokerIngestionService(store, _adapter(config))

        def archive(job):
            from .files import read_local_archive

            loaded = read_local_archive(args.file)
            if loaded.filename != config["filename"]:
                raise BrokerFlowError("import_file_context_changed")
            extension = Path(loaded.filename).suffix.lower().lstrip(".")
            return RawPayload(run_id=job.run_id, source_id=contract.source_id,
                source_contract_version=contract.source_contract_version,
                endpoint=f"offline://broker-flow/{sha256(loaded.body).hexdigest()}.{extension}",
                body=loaded.body, content_type=loaded.content_type, status_code=200,
                fetched_at=datetime.now(timezone.utc),
                request_parameters={"filename": loaded.filename, "encoding": args.encoding})

        return [service.import_file(contract, config=config, archive_factory=archive)]
    if args.resume is not None:
        job = store.get(args.resume)
        if job is None:
            raise BrokerFlowError("broker_import_job_not_found")
        return [BrokerIngestionService(store, _adapter(job.config)).resume(job.run_id)]
    context = store.context_for_raw(args.raw)
    return [BrokerIngestionService(store, _adapter(context["config"])).replay(args.raw)]


def exit_code(args, reports):
    if args.broker_flow_action == "status":
        return 0 if reports or args.id is None else 2
    return 0 if reports and all(report["status"] == "succeeded" for report in reports) else 2
