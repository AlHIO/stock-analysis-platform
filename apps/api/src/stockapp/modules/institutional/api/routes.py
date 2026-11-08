"""Bounded canonical institutional queries; GET never imports provider data."""
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from stockapp.modules.institutional.api.dependencies import get_institutional_service
from stockapp.modules.institutional.api.schemas import (
    InstitutionalHistoryResponse, InstitutionalLatestResponse,
    InstitutionalMetricsResponse, InstitutionalStatusResponse, MetricCategory, QueryDate,
)
from stockapp.modules.institutional.application.query_service import InstitutionalQueryService
from stockapp.modules.institutional.domain.models import Category
from stockapp.modules.institutional.domain.queries import InstitutionalQueryError
from stockapp.modules.market_data.api.schemas import ErrorResponse

router = APIRouter(prefix="/api/v1", tags=["Institutional trading"])
Service = Annotated[InstitutionalQueryService, Depends(get_institutional_service)]
ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Stock not found or unsupported"},
    422: {"model": ErrorResponse, "description": "Invalid or excessive institutional query"},
    503: {"model": ErrorResponse, "description": "Institutional data temporarily unavailable"},
}


@router.get("/stocks/{id}/institutional", response_model=InstitutionalHistoryResponse,
            responses=ERROR_RESPONSES, operation_id="institutional_daily_history")
def institutional_history(
    id: UUID, service: Service,
    start: Annotated[QueryDate | None, Query(alias="from")] = None,
    end: Annotated[QueryDate | None, Query(alias="to")] = None,
    trade_scope: Annotated[str | None, Query(max_length=1024,
        description="Stored scope code or strict JSON full scope; ambiguous codes require the full value.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 250,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
) -> dict:
    """Read one page of whole canonical reports with explicit lineage and quality."""
    return service.history(id, start=start, end=end, trade_scope=trade_scope,
                           limit=limit, cursor=cursor, order=order)


@router.get("/stocks/{id}/institutional/latest", response_model=InstitutionalLatestResponse,
            responses=ERROR_RESPONSES, operation_id="institutional_latest_report")
def institutional_latest(
    id: UUID, service: Service,
    trade_scope: Annotated[str | None, Query(max_length=1024)] = None,
) -> dict:
    """Return the latest saved canonical report, or point=null for an empty stock."""
    return service.latest(id, trade_scope=trade_scope)


@router.get("/stocks/{id}/institutional/metrics", response_model=InstitutionalMetricsResponse,
            responses=ERROR_RESPONSES, operation_id="institutional_trading_metrics")
def institutional_metrics(
    id: UUID, service: Service,
    start: Annotated[QueryDate | None, Query(alias="from")] = None,
    end: Annotated[QueryDate | None, Query(alias="to")] = None,
    trade_scope: Annotated[str | None, Query(max_length=1024)] = None,
    category: Annotated[MetricCategory, Query()] = "foreign_ex_dealers",
    period: Annotated[int | None, Query(json_schema_extra={"enum": [5, 10, 20, 60, None]},
        description="Confirmed sessions; omit to return all four supported windows.")] = None,
) -> dict:
    """Reuse the Domain calculator; unknown calendars never become assumed sessions."""
    return service.metrics(id, start=start, end=end, trade_scope=trade_scope,
                           category=Category(category), period=period)


@router.get("/chip-data/status", response_model=InstitutionalStatusResponse,
            responses=ERROR_RESPONSES, operation_id="institutional_source_status")
def institutional_status(
    service: Service,
    product: Annotated[Literal["institutional_daily"], Query()] = "institutional_daily",
    market: Annotated[Literal["TWSE", "TPEX"] | None, Query()] = None,
    source_code: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> dict:
    """Read saved ingestion metadata and capabilities; no public import operation."""
    return service.status(product=product, market=market, source_code=source_code,
                          limit=limit, cursor=cursor)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(InstitutionalQueryError)
    async def query_error(request: Request, exc: InstitutionalQueryError):
        code = exc.code if re.fullmatch(r"[a-z0-9_]{1,100}", exc.code) else "institutional_query_failed"
        message = "Institutional data is temporarily unavailable." if exc.status_code == 503 else exc.message
        return JSONResponse(status_code=exc.status_code,
            content=ErrorResponse(detail={"code": code, "message": message, "errors": None}).model_dump(mode="json"))

    @app.exception_handler(ResponseValidationError)
    async def invalid_response(request: Request, exc: ResponseValidationError):
        path = request.url.path
        if path == "/api/v1/chip-data/status" or re.fullmatch(
            r"/api/v1/stocks/[^/]+/institutional(?:/latest|/metrics)?", path):
            return JSONResponse(status_code=503, content=ErrorResponse(detail={
                "code": "invalid_stored_chip_data", "message": "Institutional data is temporarily unavailable.",
                "errors": None}).model_dump(mode="json"))
        return PlainTextResponse("Internal Server Error", status_code=500)
