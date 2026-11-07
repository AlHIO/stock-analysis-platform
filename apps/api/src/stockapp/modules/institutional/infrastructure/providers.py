"""Source authorization/capability gates around bounded Infrastructure HTTP.

Constructing an adapter never downloads data. Offline parsing is independent of
the gate. A runtime boolean cannot authorize a source whose rights are pending.
"""
from datetime import date
from uuid import UUID

from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError, ParsedBatch
from .source_manifest import TPEX_MANIFEST, TWSE_MANIFEST
from .tpex_parser import parse_tpex
from .twse_parser import parse_twse


class _InstitutionalAdapter:
    def __init__(self, *, network_enabled: bool = False, transport=None):
        if type(network_enabled) is not bool:
            raise ValueError("network_enabled_must_be_bool")
        if network_enabled and self.manifest.authorization == "pending":
            raise InstitutionalSourceError("source_authorization_pending")
        self.network_enabled = network_enabled
        self._transport, self._owned = transport, transport is None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self._owned and self._transport is not None:
            await self._transport.aclose()

    async def fetch(self, *, run_id: UUID, source_id: UUID, market_id: UUID,
                    trade_date: date | None = None) -> RawPayload:
        # Check on every fetch too; mutating the flag cannot unlock pending rights.
        if self.manifest.authorization == "pending":
            raise InstitutionalSourceError("source_authorization_pending")
        if not self.network_enabled:
            raise InstitutionalSourceError("network_disabled")
        if not all(isinstance(value, UUID) for value in (run_id, source_id, market_id)):
            raise InstitutionalSourceError("invalid_identity")
        parameters = self._parameters(trade_date)
        if self._transport is None:
            from .transport import InstitutionalHttpTransport
            self._transport = InstitutionalHttpTransport()
        return await self._transport.fetch(endpoint=self.manifest.endpoint,
            parameters=parameters, run_id=run_id, source_id=source_id, market_id=market_id)

    def parse(self, raw: RawPayload, *, requested_date: date | None = None) -> ParsedBatch:
        try:
            return self._parser(raw, requested_date=requested_date)
        except InstitutionalSourceError as exc:
            if exc.raw is None:
                raise InstitutionalSourceError(exc.code, raw) from exc
            raise


class TwseInstitutionalAdapter(_InstitutionalAdapter):
    manifest = TWSE_MANIFEST
    _parser = staticmethod(parse_twse)

    def _parameters(self, trade_date):
        if type(trade_date) is not date:
            raise InstitutionalSourceError("explicit_date_required")
        dates = {day for contract in self.manifest.contracts for day in contract.verified_historical_dates}
        if trade_date not in dates:
            raise InstitutionalSourceError("unverified_schema_date")
        return {"date": trade_date.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"}


class TpexInstitutionalAdapter(_InstitutionalAdapter):
    manifest = TPEX_MANIFEST
    _parser = staticmethod(parse_tpex)

    def _parameters(self, trade_date):
        if trade_date is not None:
            raise InstitutionalSourceError("historical_query_unsupported")
        return {}
