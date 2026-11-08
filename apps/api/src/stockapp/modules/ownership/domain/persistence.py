"""Persistence contracts and explicit, conservative ownership promotion profile."""
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .models import (
    AdjustmentConvention, OwnershipCompleteness, OwnershipWeeklyReport,
    _text, _uuid, report_fingerprint,
)


class PromotionDecision(StrEnum):
    PROMOTED = 'promoted'
    UNCHANGED = 'unchanged'
    STALE = 'stale'
    CANDIDATE = 'candidate'
    CONFLICT = 'conflict'


@dataclass(frozen=True, kw_only=True)
class PromotionPolicy:
    source_id: UUID
    source_contract_version: str
    scheme_version: str
    quantity_unit: str = 'shares'
    adjustment_convention: AdjustmentConvention = AdjustmentConvention.SHORTFALL_MAGNITUDE
    version: str = 'ownership-promotion-1.0.0'

    def __post_init__(self):
        _uuid(self.source_id,'source_id')
        for name in ('source_contract_version','scheme_version','version'):
            _text(getattr(self,name),name)
        if self.quantity_unit not in {'shares','units'} or not isinstance(self.adjustment_convention,AdjustmentConvention):
            raise ValueError('invalid_ownership_promotion_policy')

    def matches(self, report: OwnershipWeeklyReport):
        return (report.lineage.source_id == self.source_id
            and report.lineage.source_contract_version == self.source_contract_version
            and report.scheme.version == self.scheme_version
            and report.quantity_unit == self.quantity_unit
            and report.scheme.adjustment_convention is self.adjustment_convention
            and report.completeness is OwnershipCompleteness.COMPLETE
            and report.lineage.identity_evidence_version not in {'unverified','unknown'})


@dataclass(frozen=True, kw_only=True)
class OwnershipWriteResult:
    revision_id: UUID
    observation_id: UUID
    decision: PromotionDecision
    reason: str
    revision_created: bool
    canonical_changed: bool
    canonical: OwnershipWeeklyReport | None
    duplicate_observation: bool = False


class OwnershipRepository(Protocol):
    def upsert(self, report: OwnershipWeeklyReport, *, row_index: int, raw_rows: tuple[dict, ...] = ()) -> OwnershipWriteResult: ...
    def get_canonical(self, instrument_id: UUID, as_of_date: date) -> OwnershipWeeklyReport | None: ...
    def list_revisions(self, instrument_id: UUID, as_of_date: date) -> tuple[OwnershipWeeklyReport, ...]: ...
