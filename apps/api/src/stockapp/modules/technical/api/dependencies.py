"""Composition uses the market module's public query application dependency."""
from typing import Annotated
from fastapi import Depends

from stockapp.modules.market_data.api.dependencies import get_query_service
from stockapp.modules.market_data.application.indicator_history import MarketIndicatorHistoryService
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.technical.application.service import TechnicalIndicatorService
from stockapp.modules.technical.domain.engine import DecimalIndicatorEngine
from stockapp.modules.technical.infrastructure.history import MarketDataIndicatorHistorySource


def get_indicator_service(queries: Annotated[MarketQueryService, Depends(get_query_service)]) -> TechnicalIndicatorService:
    return TechnicalIndicatorService(MarketDataIndicatorHistorySource(MarketIndicatorHistoryService(queries)), DecimalIndicatorEngine())
