"""Saved Canonical broker reports and versioned calculations; GET performs no fetch."""
from enum import IntEnum, StrEnum
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stockapp.modules.broker_flow.domain.engine import BROKER_METRICS
from stockapp.modules.broker_flow.domain.query import BrokerQueryError
from stockapp.modules.market_data.public_api import ErrorResponse
from .dependencies import get_broker_query_service
from .schemas import (
    BrokerAvailabilityPolicy, BrokerHistoryResponse, BrokerLatestResponse, BrokerMetricsResponse,
    BrokerStatusResponse, BrokerTopBranchesResponse, QueryClock, QueryDate,
)


BrokerAlgorithm = StrEnum("BrokerAlgorithm", {
    key.replace(".", "_"): key for key in BROKER_METRICS
})


class BrokerTopN(IntEnum):
    FIVE = 5
    TEN = 10
    FIFTEEN = 15
    THIRTY = 30


class BrokerSessionCount(IntEnum):
    ONE = 1
    FIVE = 5
    TEN = 10
    TWENTY = 20
    SIXTY = 60


router = APIRouter(prefix="/api/v1", tags=["Broker flow"])
Service = Annotated[object, Depends(get_broker_query_service)]
ERRORS = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}


def _validated(schema, value):
    try:
        return schema.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise BrokerQueryError("invalid_stored_broker_data", "Broker data is temporarily unavailable.", 503) from None


@router.get("/stocks/{id}/broker-flow", response_model=BrokerHistoryResponse,
            responses=ERRORS, operation_id="broker_flow_daily_history")
def history(id: UUID, service: Service,
    start: Annotated[QueryDate | None, Query(alias="from")] = None,
    end: Annotated[QueryDate | None, Query(alias="to")] = None,
    timeframe: Literal["1d"] = "1d",
    trade_scope: UUID | None = None,
    source: UUID | None = None,
    broker: UUID | None = None,
    branch: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    order: Literal["asc", "desc"] = "asc"):
    """Whole saved source/scope reports; filters never imply full-market coverage."""
    return _validated(BrokerHistoryResponse, service.history(id, start=start, end=end,
        trade_scope=trade_scope, source=source, broker=broker, branch=branch,
        timeframe=timeframe, limit=limit, cursor=cursor, order=order))


@router.get("/stocks/{id}/broker-flow/latest", response_model=BrokerLatestResponse,
            responses=ERRORS, operation_id="broker_flow_latest_report")
def latest(id: UUID, service: Service,
    trade_scope: UUID | None = None,
    source: UUID | None = None,
    broker: UUID | None = None,
    branch: UUID | None = None):
    """Latest stored report in one selected scope; never an official-live claim."""
    return _validated(BrokerLatestResponse, service.latest(id, trade_scope=trade_scope,
        source=source, broker=broker, branch=branch))


@router.get("/stocks/{id}/broker-flow/top-branches", response_model=BrokerTopBranchesResponse,
            responses=ERRORS, operation_id="broker_flow_top_branches")
def top_branches(id: UUID, service: Service,
    start: Annotated[QueryDate | None, Query(alias="from")] = None,
    end: Annotated[QueryDate | None, Query(alias="to")] = None,
    trade_scope: UUID | None = None,
    source: UUID | None = None,
    top_n: BrokerTopN = BrokerTopN.FIFTEEN,
    sessions: BrokerSessionCount | None = None,
    side: Literal["net_buyers", "net_sellers"] = "net_buyers",
    broker: UUID | None = None,
    branch: UUID | None = None):
    """Aggregate the full compatible universe before ranking and output filtering."""
    return _validated(BrokerTopBranchesResponse, service.top_branches(id,
        start=start, end=end, trade_scope=trade_scope, source=source,
        top_n=int(top_n), side=side, broker=broker, branch=branch,
        sessions=int(sessions) if sessions is not None else None))


@router.get("/stocks/{id}/broker-flow/metrics", response_model=BrokerMetricsResponse,
            responses=ERRORS, operation_id="broker_flow_metrics")
def metrics(id: UUID, service: Service,
    algorithm: BrokerAlgorithm = BrokerAlgorithm.broker_source_coverage,
    algorithm_parameters: Annotated[str | None, Query(max_length=4096)] = None,
    start: Annotated[QueryDate | None, Query(alias="from")] = None,
    end: Annotated[QueryDate | None, Query(alias="to")] = None,
    timeframe: Literal["1d"] = "1d",
    trade_scope: UUID | None = None,
    source: UUID | None = None,
    broker: UUID | None = None,
    branch: UUID | None = None,
    query_as_of: Annotated[QueryClock | None, Query()] = None,
    availability_policy: BrokerAvailabilityPolicy = BrokerAvailabilityPolicy.OBSERVED):
    """Delegate exact parameters to H4; unsupported or incomplete values stay null."""
    return _validated(BrokerMetricsResponse, service.metrics(id, algorithm=algorithm.value,
        algorithm_parameters=algorithm_parameters, start=start, end=end,
        trade_scope=trade_scope, source=source, broker=broker, branch=branch,
        query_as_of=query_as_of, availability_policy=availability_policy.value, timeframe=timeframe))


@router.get("/broker-flow/status", response_model=BrokerStatusResponse,
            responses=ERRORS, operation_id="broker_flow_ingestion_status")
def status(service: Service,
    source: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """Saved counts and sanitized ingestion outcomes; download/display remain disabled."""
    return _validated(BrokerStatusResponse, service.status(source=source, limit=limit))


def install_error_handlers(app: FastAPI):
    @app.exception_handler(BrokerQueryError)
    async def broker_error(request: Request, exc: BrokerQueryError):
        status = exc.status_code if exc.status_code in (404, 422, 503) else 503
        code = (exc.code if isinstance(exc.code, str) and re.fullmatch(r"[a-z0-9_]{1,100}", exc.code)
                else "broker_query_failed")
        message = "Broker data is temporarily unavailable." if status == 503 else exc.message
        return JSONResponse(status_code=status, content=ErrorResponse(detail={
            "code": code, "message": message, "errors": None,
        }).model_dump(mode="json"))
