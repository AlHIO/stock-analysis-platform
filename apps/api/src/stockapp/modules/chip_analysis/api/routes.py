"""Conservative historical and cross-frequency chip metrics; GET never fetches."""
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stockapp.modules.chip_analysis.public import AvailabilityPolicy, Metric
from stockapp.modules.chip_analysis.domain.queries import ChipQueryError
from stockapp.modules.lending.public import ReportKind
from stockapp.modules.market_data.public_api import ErrorResponse
from .dependencies import get_chip_query_service
from .schemas import ChipMetricsResponse, QueryClock, QueryDate

router = APIRouter(prefix='/api/v1', tags=['Chip metrics'])
Service = Annotated[object, Depends(get_chip_query_service)]
ERRORS = {404: {'model': ErrorResponse}, 422: {'model': ErrorResponse}, 503: {'model': ErrorResponse}}


def parse_parameters(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise ChipQueryError('invalid_metric_parameters', 'Parameters must be a bounded JSON object.')

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = item
        return result

    def invalid_number(value):
        raise ValueError('floating-point and nonfinite numbers are not permitted')

    def check(item, depth=0):
        if depth > 4:
            raise ValueError('excessive parameter nesting')
        if isinstance(item, dict):
            if len(item) > 100:
                raise ValueError('excessive parameter count')
            for key, member in item.items():
                if not isinstance(key, str):
                    raise ValueError('invalid parameter key')
                check(member, depth+1)
        elif type(item) is int:
            if not -(2**63) <= item <= 2**63-1:
                raise ValueError('integer outside exact source limits')
        elif not isinstance(item, str):
            raise ValueError('parameters must be exact integers, strings or objects')

    try:
        document = json.loads(value, object_pairs_hook=pairs, parse_float=invalid_number,
                              parse_constant=invalid_number)
        if not isinstance(document, dict):
            raise ValueError('parameters must be an object')
        check(document)
        return document
    except (ValueError, TypeError, RecursionError):
        raise ChipQueryError('invalid_metric_parameters', 'Metric parameters are invalid.') from None


@router.get('/stocks/{id}/chip-metrics', response_model=ChipMetricsResponse,
            responses=ERRORS, operation_id='chip_metrics_history')
def history(id: UUID, service: Service,
    metric: Annotated[list[Metric], Query(min_length=1, max_length=10)],
    evaluation_frequency: Literal['1d', '1w'] = '1d',
    parameters: Annotated[str | None, Query(max_length=4096)] = None,
    start: Annotated[QueryDate | None, Query(alias='from')] = None,
    end: Annotated[QueryDate | None, Query(alias='to')] = None,
    query_as_of: Annotated[QueryClock | None, Query()] = None,
    time_basis: Literal['as_of_day', 'query_as_of'] = 'as_of_day',
    availability_policy: AvailabilityPolicy = AvailabilityPolicy.OBSERVED,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    institutional_scope: Annotated[str | None, Query(min_length=1, max_length=1000)] = None,
    margin_scope: Annotated[str, Query(min_length=1, max_length=100)] = 'margin_trading',
    lending_scope: Annotated[str, Query(min_length=1, max_length=100)] = 'unspecified',
    lending_channel: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    channel_family: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    report_kind: ReportKind = ReportKind.LENDING_SHORT_SALES):
    """Current canonical revisions with explicit clocks, never a PIT guarantee."""
    if len(set(metric)) != len(metric):
        raise ChipQueryError('duplicate_metric', 'Each metric may be requested only once.')
    values = service.query(id, metrics=tuple(metric), evaluation_frequency=evaluation_frequency,
        parameters=parse_parameters(parameters), start=start, end=end, query_as_of=query_as_of,
        time_basis=time_basis, availability_policy=availability_policy, limit=limit, cursor=cursor,
        institutional_scope=institutional_scope, margin_scope=margin_scope,
        lending_scope=lending_scope, lending_channel=lending_channel,
        channel_family=channel_family, report_kind=report_kind)
    try:
        return ChipMetricsResponse.model_validate(values)
    except (ValidationError, ValueError, TypeError):
        raise ChipQueryError('invalid_stored_chip_data', 'Chip metrics are temporarily unavailable.', 503) from None


def install_error_handlers(app: FastAPI):
    @app.exception_handler(ChipQueryError)
    async def chip_error(request: Request, exc: ChipQueryError):
        status = exc.status_code if exc.status_code in (404, 422, 503) else 503
        code = exc.code if isinstance(exc.code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', exc.code) else 'chip_query_failed'
        message = 'Chip metrics are temporarily unavailable.' if status == 503 else exc.message
        return JSONResponse(status_code=status, content=ErrorResponse(detail={
            'code': code, 'message': message, 'errors': None,
        }).model_dump(mode='json'))
