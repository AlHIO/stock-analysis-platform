"""Public identity lookup boundary; persistence ports live in persistence.py."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import BrokerBranchIdentityHistory, BrokerIdentityClosure
from .analytics import (
    BrokerAnalysisHistory, BrokerMetricRequest, BrokerMetricResult, PriceObservation,
)


class BrokerIdentityReader(Protocol):
    def get_identity_closure(self, identity_id: UUID) -> BrokerIdentityClosure | None:
        """Return recorded closure evidence; an unknown edge stays None."""
        ...

    def resolve(self, *, source_id: UUID, market_id: UUID, source_namespace: str,
                branch_code: str, trade_date: date,
                identity_version: str | None = None) -> BrokerBranchIdentityHistory | None:
        """Return an exact verified effective mapping; never infer a near match."""
        ...


class BrokerAnalysisReader(Protocol):
    def load(self, request: BrokerMetricRequest) -> BrokerAnalysisHistory:
        """Load bounded canonical facts in a caller-owned consistent snapshot.

        Keep source/scope, dated identities, revision and publication evidence.
        Do not download providers, infer a calendar or manufacture missing data.
        """
        ...


class BrokerPriceReader(Protocol):
    def read_prices(self, *, instrument_id: UUID, market_id: UUID, start: date,
                    end: date, price_basis: str) -> tuple[PriceObservation, ...]:
        """Read one explicitly selected price series through a public boundary.

        Return exact closes and input revisions with the original local dates;
        raw and adjusted prices are separate series, with no implicit filling.
        """
        ...


class BrokerCalculationEngine(Protocol):
    def calculate(self, request: BrokerMetricRequest,
                  history: BrokerAnalysisHistory) -> BrokerMetricResult: ...
