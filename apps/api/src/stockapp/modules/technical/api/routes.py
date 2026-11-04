"""Bounded indicators computed exclusively from saved canonical history."""
from datetime import date
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from stockapp.modules.market_data.api.routes import ERROR_RESPONSES
from stockapp.modules.market_data.domain.queries import InvalidQuery, Timeframe
from stockapp.modules.technical.api.dependencies import get_indicator_service
from stockapp.modules.technical.api.schemas import IndicatorName, IndicatorResponse
from stockapp.modules.technical.application.service import TechnicalIndicatorService

router = APIRouter(prefix="/api/v1", tags=["Technical indicators"])


def parse_parameters(raw: str | None):
    if raw is None:
        return None
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate parameter")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if not isinstance(result, dict):
            raise ValueError("parameters must be an object")
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidQuery("invalid_indicator_parameters", "parameters must be a JSON object with unique integer parameter names") from exc


@router.get("/stocks/{id}/indicators", response_model=IndicatorResponse, responses=ERROR_RESPONSES)
def indicators(
    id: UUID,
    service: Annotated[TechnicalIndicatorService, Depends(get_indicator_service)],
    timeframe: Annotated[Timeframe, Query()] = "1d",
    indicator: Annotated[IndicatorName, Query()] = "sma",
    parameters: Annotated[str | None, Query(max_length=512, description='Strict JSON integer parameters, e.g. {"period":20}.')] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    price_basis: Annotated[Literal["raw", "adjusted"], Query(description="Adjusted history is currently unavailable and returns 422.")] = "raw",
) -> dict:
    """Compute full saved-history seeds, then filter actual output dates. No provider requests."""
    return service.calculate(id, timeframe=timeframe, indicator=indicator,
                             parameters=parse_parameters(parameters), start=start, end=end, price_basis=price_basis)
