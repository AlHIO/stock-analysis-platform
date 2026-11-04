"""Adapt market_data's public application facade to the technical input port."""
from stockapp.modules.market_data.application.indicator_history import MarketIndicatorHistoryService
from stockapp.modules.technical.domain.history import IndicatorHistorySnapshot
from stockapp.modules.technical.domain.models import IndicatorSample


class MarketDataIndicatorHistorySource:
    def __init__(self, market: MarketIndicatorHistoryService):
        self.market = market

    def load(self, instrument_id, *, timeframe, start, end):
        snapshot = self.market.load(instrument_id, timeframe=timeframe, start=start, end=end)
        observations = snapshot.pop("observations")
        return IndicatorHistorySnapshot(**snapshot, samples=tuple(IndicatorSample(**point) for point in observations))
