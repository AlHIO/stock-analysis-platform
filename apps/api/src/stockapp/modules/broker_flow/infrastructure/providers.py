"""Offline interchange adapter and a disabled licensed-provider extension point."""
from ..domain.models import BrokerFlowError
from .source_manifest import MANIFESTS


class OfflineBrokerAdapter:
    def __init__(self, *, filename: str, encoding: str = "utf-8"):
        self.filename = filename
        self.encoding = encoding

    def parse(self, raw):
        from .parsers import parse_archive

        return parse_archive(raw.body, self.filename, raw_id=raw.id,
                             raw_sha256=raw.sha256, encoding=self.encoding)


class LicensedBrokerAdapter:
    """Reserved provider port; no official download pipeline is enabled.

    Adding a real implementation requires a separately reviewed manifest,
    delivery contract and literal parser. A caller-supplied flag cannot enable it.
    """

    def __init__(self, source_key: str):
        if source_key not in MANIFESTS:
            raise BrokerFlowError("broker_source_not_supported")
        self.manifest = MANIFESTS[source_key]

    def fetch(self, *, source_contract_version: str):
        raise BrokerFlowError("broker_download_disabled")
