"""Exact margin calculations with immutable source and policy references."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .models import (
    ConversionEvidence, FormulaPolicy, MarginSourceLineage, Quantity,
    QuantityUnit, SourceTiming,
)

ALGORITHM_VERSION = 'margin-calculator-1.0.0'
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
    timing: SourceTiming
    lineage: MarginSourceLineage
    financing_policy: FormulaPolicy
    short_margin_policy: FormulaPolicy


@dataclass(frozen=True, kw_only=True)
class CalculationResult:
    """An unavailable result never carries a usable value of zero.

    For an identity, expected/reported/difference expose the reconciliation:
    difference = reported - expected. A mismatch has status INVALID, value=None.
    For quota, a negative expected headroom remains diagnostic, not an executable
    available quota. No rounding, clamping or implicit adjustment is performed.
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
    reasons: tuple[str, ...] = ()
    input_quantity: Quantity | None = None
    conversion_evidence: ConversionEvidence | None = None
    algorithm_version: str = ALGORITHM_VERSION
    decimal_precision: int = DECIMAL_PRECISION
