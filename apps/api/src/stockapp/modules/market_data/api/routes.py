"""Database-backed stock queries; provider network calls are never made here."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from stockapp.modules.market_data.api.dependencies import get_query_service
from stockapp.modules.market_data.api.schemas import (
    BarsResponse,
    DataStatusResponse,
    ErrorResponse,
    InstrumentSchema,
    LatestResponse,
    SearchResponse,
)
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import InstrumentType
from stockapp.modules.market_data.domain.queries import (
    MAX_OUTPUT_BARS,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    STATUS_LIMIT,
    InvalidQuery,
    InvalidStoredData,
    ResourceNotFound,
    Timeframe,
)


router = APIRouter(prefix="/api/v1", tags=["Market queries"])
QueryService = Annotated[MarketQueryService, Depends(get_query_service)]
ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Instrument not found"},
    422: {"model": ErrorResponse, "description": "Invalid or excessive query"},
    503: {"model": ErrorResponse, "description": "Market data temporarily unavailable"},
}


@router.get("/instruments", response_model=SearchResponse, responses=ERROR_RESPONSES)
def search_instruments(
    service: QueryService,
    q: Annotated[str | None, Query(max_length=100)] = None,
    symbol: Annotated[str | None, Query(max_length=32)] = None,
    name: Annotated[str | None, Query(max_length=200)] = None,
    market: Annotated[str | None, Query(max_length=32)] = None,
    instrument_type: Annotated[InstrumentType | None, Query(description="Filter security type before pagination; omit for all types.")] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> dict:
    """Search stored securities; filters combine with AND and q matches symbol or name."""
    return service.search(q=q, symbol=symbol, name=name, market=market,
                          instrument_type=instrument_type, page=page, page_size=page_size)


@router.get("/instruments/{id}", response_model=InstrumentSchema, responses=ERROR_RESPONSES)
def get_instrument(id: UUID, service: QueryService) -> dict:
    return service.instrument(id)


@router.get("/stocks/{id}/bars", response_model=BarsResponse, responses=ERROR_RESPONSES)
def get_bars(
    id: UUID,
    service: QueryService,
    timeframe: Annotated[Timeframe, Query(description="Exchange-local daily, ISO weekly, or calendar monthly bars.")] = "1d",
    start: Annotated[date | None, Query(alias="from", description="Inclusive exchange-local date.")] = None,
    end: Annotated[date | None, Query(alias="to", description="Inclusive exchange-local date.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_OUTPUT_BARS)] = MAX_OUTPUT_BARS,
) -> dict:
    """Read unadjusted historical snapshots with explicit coverage and freshness metadata."""
    return service.bars(id, timeframe=timeframe, start=start, end=end, limit=limit)


@router.get("/stocks/{id}/latest", response_model=LatestResponse, responses=ERROR_RESPONSES)
def get_latest(id: UUID, service: QueryService) -> dict:
    """Return the latest stored daily snapshot. This endpoint does not provide live prices."""
    return service.latest(id)


@router.get("/data/status", response_model=DataStatusResponse, responses=ERROR_RESPONSES)
def get_data_status(
    service: QueryService,
    limit: Annotated[int, Query(ge=1, le=STATUS_LIMIT)] = STATUS_LIMIT,
) -> dict:
    """Summarize stored coverage and ingestion runs without fetching any providers."""
    return service.status(limit=limit)


def _error(status: int, code: str, message: str, errors: list[dict] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(detail={"code": code, "message": message, "errors": errors}).model_dump(mode="json"),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Keep validation and database errors stable without exposing inputs or DSNs."""

    @app.exception_handler(ResourceNotFound)
    async def not_found(request: Request, exc: ResourceNotFound) -> JSONResponse:
        return _error(404, exc.code, exc.message)

    @app.exception_handler(InvalidQuery)
    async def invalid_query(request: Request, exc: InvalidQuery) -> JSONResponse:
        return _error(422, exc.code, exc.message)

    @app.exception_handler(InvalidStoredData)
    async def invalid_stored_data(request: Request, exc: InvalidStoredData) -> JSONResponse:
        return _error(503, "invalid_stored_data", "Stored market data is temporarily unavailable.")

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return _error(503, "database_unavailable", "Market data is temporarily unavailable.")

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [{"loc": list(error["loc"]), "message": error["msg"], "type": error["type"]} for error in exc.errors()]
        return _error(422, "invalid_request", "Request parameters are invalid.", issues)
