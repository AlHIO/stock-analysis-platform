"""Explicit opt-in TDCC CSV Adapter; constructing or parsing never downloads."""
from stockapp.modules.ownership.domain.models import OwnershipError
from .source_manifest import TDCC_MANIFEST, TDCC_PARAMETERS
from .tdcc_parser import parse_tdcc_csv
from .transport import OwnershipHttpTransport


class TdccOwnershipAdapter:
    manifest = TDCC_MANIFEST

    def __init__(self, *, network_enabled=False, transport=None):
        if type(network_enabled) is not bool:
            raise ValueError('network_enabled must be boolean')
        self.network_enabled = network_enabled
        self.transport, self.owned = transport, transport is None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self.owned and self.transport is not None:
            await self.transport.aclose()

    async def fetch(self, *, run_id, source_id, on_response=None, as_of_date=None):
        if self.manifest.authorization != 'ogdl_dataset_documented':
            raise OwnershipError('source_authorization_pending')
        if not self.network_enabled:
            raise OwnershipError('network_disabled')
        if as_of_date is not None:
            raise OwnershipError('historical_query_unsupported')
        if self.transport is None:
            self.transport = OwnershipHttpTransport()
        return await self.transport.fetch(endpoint=self.manifest.endpoint, parameters=dict(TDCC_PARAMETERS),
            run_id=run_id, source_id=source_id, on_response=on_response)

    def parse(self, raw):
        try:
            return parse_tdcc_csv(raw)
        except OwnershipError as error:
            error.raw = raw
            raise


# Consistent acronym spelling for callers that prefer the provider's name.
TDCCOwnershipAdapter = TdccOwnershipAdapter
