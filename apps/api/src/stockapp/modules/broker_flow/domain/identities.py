"""Exact, market-local identity selection; no fuzzy aliases or succession rollup."""
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from .models import (
    BrokerBranchIdentityHistory, BrokerFlowError, BrokerTradingDailyReport,
    _date, _text, _uuid,
)


def _overlap(left: BrokerBranchIdentityHistory, right: BrokerBranchIdentityHistory) -> bool:
    return ((right.valid_to is None or left.valid_from < right.valid_to)
            and (left.valid_to is None or right.valid_from < left.valid_to))


@dataclass(frozen=True, kw_only=True)
class BrokerIdentityBook:
    """A snapshot with nonoverlapping verified codes and branch assignments.

    Absence returns None. Case, namespaces, markets and dates must all match;
    a successor UUID is descriptive history and is never followed implicitly.
    """
    identities: tuple[BrokerBranchIdentityHistory, ...]

    def __post_init__(self):
        if not isinstance(self.identities, (tuple, list)) or not all(
                isinstance(identity, BrokerBranchIdentityHistory) for identity in self.identities):
            raise BrokerFlowError("invalid_identity_book")
        object.__setattr__(self, "identities", tuple(self.identities))
        if len({identity.id for identity in self.identities}) != len(self.identities):
            raise BrokerFlowError("duplicate_identity_id")
        groups: dict[tuple, list[BrokerBranchIdentityHistory]] = {}
        for identity in self.identities:
            if not identity.verified:
                continue
            groups.setdefault(("code", *identity.mapping_key), []).append(identity)
            groups.setdefault(("branch", identity.source_id, identity.market_id,
                               identity.source_namespace, identity.branch_id), []).append(identity)
        for group in groups.values():
            ordered = sorted(group, key=lambda identity: identity.valid_from)
            for left, right in zip(ordered, ordered[1:]):
                if _overlap(left, right):
                    raise BrokerFlowError("overlapping_identity_mapping")

    def resolve(self, *, source_id: UUID, market_id: UUID, source_namespace: str,
                branch_code: str, trade_date: date,
                identity_version: str | None = None) -> BrokerBranchIdentityHistory | None:
        _uuid(source_id, "source_id")
        _uuid(market_id, "market_id")
        _text(source_namespace, "source_namespace")
        _text(branch_code, "branch_code")
        _date(trade_date, "trade_date")
        if identity_version is not None:
            _text(identity_version, "identity_version")
        key = source_id, market_id, source_namespace, branch_code
        return next((identity for identity in self.identities
                     if identity.verified and identity.mapping_key == key
                     and (identity_version is None or identity.identity_version == identity_version)
                     and identity.effective_on(trade_date)), None)

    def validate_report(self, report: BrokerTradingDailyReport) -> None:
        if not isinstance(report, BrokerTradingDailyReport):
            raise BrokerFlowError("invalid_report")
        by_id = {identity.id: identity for identity in self.identities}
        for trade in report.trades:
            identity = by_id.get(trade.identity_id)
            if identity is None or not identity.verified:
                raise BrokerFlowError("unresolved_branch_identity")
            if (identity.branch_id != trade.branch_id
                    or identity.market_id != report.market_id
                    or identity.source_id != report.lineage.source_id
                    or identity.source_namespace != report.lineage.source_namespace
                    or not identity.effective_on(report.trade_date)):
                raise BrokerFlowError("incompatible_branch_identity")
