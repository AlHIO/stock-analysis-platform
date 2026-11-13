"""Public identity lookup boundary; persistence ports live in persistence.py."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import BrokerBranchIdentityHistory, BrokerIdentityClosure


class BrokerIdentityReader(Protocol):
    def get_identity_closure(self, identity_id: UUID) -> BrokerIdentityClosure | None:
        """Return recorded closure evidence; an unknown edge stays None."""
        ...

    def resolve(self, *, source_id: UUID, market_id: UUID, source_namespace: str,
                branch_code: str, trade_date: date,
                identity_version: str | None = None) -> BrokerBranchIdentityHistory | None:
        """Return an exact verified effective mapping; never infer a near match."""
        ...
