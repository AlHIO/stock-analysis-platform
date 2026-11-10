"""Canonical public inputs only; callers own I/O and transaction snapshots."""
from typing import Protocol
from .models import AnalysisRequest, AnalyticalResult, AnalyticalSnapshot


class ChipInputReader(Protocol):
    def load(self, request: AnalysisRequest) -> AnalyticalSnapshot:
        """Bounded canonical read via product public interfaces.

        Select one revision per natural key in one consistent read transaction;
        no provider fetch, private table access, implicit commits or writes.
        Preserve calendar/schedule coverage and original dates/publication times.
        The engine checks every actually used revision's availability again.
        """
        ...


class ChipCalculationEngine(Protocol):
    def calculate(self, request: AnalysisRequest, snapshot: AnalyticalSnapshot) -> AnalyticalResult: ...
