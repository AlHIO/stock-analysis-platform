"""Input port for stored, explicitly labelled indicator history."""
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol
from uuid import UUID

from stockapp.modules.technical.domain.models import IndicatorSample


@dataclass(frozen=True)
class IndicatorHistorySnapshot:
    instrument: dict[str, Any]
    timeframe: Literal["1d", "1w", "1M"]
    start: date
    end: date
    samples: tuple[IndicatorSample, ...]
    history_from: date | None
    freshness: dict[str, Any]
    history: dict[str, Any]


class IndicatorHistorySource(Protocol):
    def load(self, instrument_id: UUID, *, timeframe: str, start: date | None,
             end: date | None) -> IndicatorHistorySnapshot: ...
