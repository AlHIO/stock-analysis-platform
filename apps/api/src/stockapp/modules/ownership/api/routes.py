"""Bounded, weekly canonical ownership queries; GET never contacts TDCC."""
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator

from stockapp.modules.market_data.api.schemas import ErrorResponse
from stockapp.modules.ownership.api.dependencies import get_ownership_service
from stockapp.modules.ownership.api.schemas import (
    BIGINT_MAX,OwnershipDistributionResponse,OwnershipHistoryResponse,
    OwnershipLatestResponse,OwnershipMetricsResponse,QueryDate,
)
from stockapp.modules.ownership.application.query_service import OwnershipQueryService
from stockapp.modules.ownership.domain.queries import OwnershipQueryError

router=APIRouter(prefix='/api/v1',tags=['Ownership distribution'])
Service=Annotated[OwnershipQueryService,Depends(get_ownership_service)]
ERROR_RESPONSES={
    404:{'model':ErrorResponse,'description':'Instrument not found or unsupported'},
    422:{'model':ErrorResponse,'description':'Invalid or excessive ownership query'},
    503:{'model':ErrorResponse,'description':'Ownership data temporarily unavailable'},
}


def _threshold(value):
    if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,18}',value):
        return int(value)
    if type(value) is int:
        return value
    raise ValueError('Use a positive integer threshold in base shares or units')


Threshold=Annotated[int,BeforeValidator(_threshold)]


@router.get('/stocks/{id}/ownership',response_model=OwnershipHistoryResponse,
    responses=ERROR_RESPONSES,operation_id='ownership_weekly_history')
def ownership_history(
    id:UUID,service:Service,
    start:Annotated[QueryDate|None,Query(alias='from')]=None,
    end:Annotated[QueryDate|None,Query(alias='to')]=None,
    limit:Annotated[int,Query(ge=1,le=1000)]=250,
    cursor:Annotated[str|None,Query(max_length=2048)]=None,
    order:Annotated[Literal['asc','desc'],Query()]='asc',
) -> dict:
    """Return complete stored weekly snapshots without splitting a report's buckets."""
    return service.history(id,start=start,end=end,limit=limit,cursor=cursor,order=order)


@router.get('/stocks/{id}/ownership/latest',response_model=OwnershipLatestResponse,
    responses=ERROR_RESPONSES,operation_id='ownership_latest_report')
def ownership_latest(id:UUID,service:Service) -> dict:
    """Latest saved canonical snapshot; point=null when no report is stored."""
    return service.latest(id)


@router.get('/stocks/{id}/ownership/distribution',response_model=OwnershipDistributionResponse,
    responses=ERROR_RESPONSES,operation_id='ownership_bucket_distribution')
def ownership_distribution(
    id:UUID,service:Service,
    as_of:Annotated[QueryDate|None,Query(description='Exact statistics date; omit for latest saved report.')]=None,
) -> dict:
    """Show regular, adjustment and total rows separately for one exact snapshot."""
    return service.distribution(id,as_of=as_of)


@router.get('/stocks/{id}/ownership/metrics',response_model=OwnershipMetricsResponse,
    responses=ERROR_RESPONSES,operation_id='ownership_large_holder_metrics')
def ownership_metrics(
    id:UUID,service:Service,
    start:Annotated[QueryDate|None,Query(alias='from')]=None,
    end:Annotated[QueryDate|None,Query(alias='to')]=None,
    threshold:Annotated[Threshold,Query(ge=1,le=BIGINT_MAX,
        description='Inclusive threshold in base quantity. A cutoff inside a bucket is unsupported; e.g. 400001 shares is exact, 400000 is not.')]=400001,
    threshold_unit:Annotated[Literal['shares','units'],Query()]='shares',
    limit:Annotated[int,Query(ge=1,le=1000)]=250,
    cursor:Annotated[str|None,Query(max_length=2048)]=None,
    order:Annotated[Literal['asc','desc'],Query()]='asc',
) -> dict:
    """Exact bucket-aligned ratios and source-confirmed weekly percentage-point changes."""
    return service.metrics(id,start=start,end=end,threshold=threshold,threshold_unit=threshold_unit,
        limit=limit,cursor=cursor,order=order)


def install_error_handlers(app:FastAPI) -> None:
    @app.exception_handler(OwnershipQueryError)
    async def ownership_query_error(request:Request,exc:OwnershipQueryError):
        code=exc.code if re.fullmatch(r'[a-z0-9_]{1,100}',exc.code) else 'ownership_query_failed'
        message='Ownership data is temporarily unavailable.' if exc.status_code==503 else exc.message
        return JSONResponse(status_code=exc.status_code,content=ErrorResponse(detail={
            'code':code,'message':message,'errors':None}).model_dump(mode='json'))
