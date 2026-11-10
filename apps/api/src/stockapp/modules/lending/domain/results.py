"""Exact lending results retain source, channel, timing and formula provenance."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .models import (
    ChannelDefinition, ChannelPartition, FormulaPolicy, LendingSourceLineage, QuantityUnit,
    ReportKind, SourceTiming,
)

ALGORITHM_VERSION = 'lending-calculator-1.0.0'
DECIMAL_PRECISION = 50


class CalculationStatus(StrEnum):
    COMPLETE = 'complete'
    MISSING_DATA = 'missing_data'
    NOT_APPLICABLE = 'not_applicable'
    NOT_VERIFIED = 'not_verified'
    INCOMPATIBLE_DATA = 'incompatible_data'
    INVALID = 'invalid'


@dataclass(frozen=True, kw_only=True)
class CalculationInput:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    report_kind: ReportKind
    quantity_unit: QuantityUnit
    channel: ChannelDefinition
    timing: SourceTiming
    lineage: LendingSourceLineage
    formula_policy: FormulaPolicy


@dataclass(frozen=True, kw_only=True)
class CalculationResult:
    """Unknown is null; an identity mismatch exposes reported - expected.

    A channel sum is a single-date stock balance with explicit partition
    evidence. It never represents daily volume or sums an overlapping total.
    """
    metric: str
    status: CalculationStatus
    value: Decimal | None
    quantity_unit: QuantityUnit
    formula: str
    input_refs: tuple[CalculationInput, ...] = ()
    expected: Decimal | None = None
    reported: Decimal | None = None
    difference: Decimal | None = None
    missing_fields: tuple[str, ...] = ()
    missing_channels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    partition_version: str | None = None
    partition_evidence_version: str | None = None
    partition_evidence: ChannelPartition | None = None
    algorithm_version: str = ALGORITHM_VERSION
    decimal_precision: int = DECIMAL_PRECISION
