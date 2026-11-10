"""Separate canonical borrowing and lending-short series, never provider GETs."""
import re
from typing import Annotated,Literal
from uuid import UUID

from fastapi import APIRouter,Depends,FastAPI,Query,Request
from fastapi.responses import JSONResponse

from stockapp.modules.market_data.api.schemas import ErrorResponse
from stockapp.modules.lending.application.query_service import LendingQueryService
from stockapp.modules.lending.domain.models import ReportKind
from stockapp.modules.lending.domain.queries import LendingQueryError
from .dependencies import get_lending_query_service
from .schemas import LendingHistoryResponse,LendingLatestResponse,LendingStatusResponse,QueryDate

router=APIRouter(prefix='/api/v1',tags=['Securities lending'])
Service=Annotated[LendingQueryService,Depends(get_lending_query_service)]
ERRORS={404:{'model':ErrorResponse},422:{'model':ErrorResponse},503:{'model':ErrorResponse}}


@router.get('/stocks/{id}/lending',response_model=LendingHistoryResponse,responses=ERRORS,
            operation_id='lending_daily_history')
def history(id:UUID,service:Service,
    report_kind:ReportKind=ReportKind.LENDING_SHORT_SALES,
    start:Annotated[QueryDate|None,Query(alias='from')]=None,
    end:Annotated[QueryDate|None,Query(alias='to')]=None,
    trade_scope:Annotated[str,Query(max_length=100)]='unspecified',
    lending_channel:Annotated[str|None,Query(min_length=1,max_length=256)]=None,
    channel_family:Annotated[str|None,Query(min_length=1,max_length=256)]=None,
    limit:Annotated[int,Query(ge=1,le=1000)]=250,
    cursor:Annotated[str|None,Query(max_length=2048)]=None,
    order:Literal['asc','desc']='asc'):
    """Separate typed points per date/channel; no balance or channel aggregation."""
    return service.history(id,report_kind=report_kind,start=start,end=end,trade_scope=trade_scope,
        lending_channel=lending_channel,channel_family=channel_family,limit=limit,cursor=cursor,order=order)


@router.get('/stocks/{id}/lending/latest',response_model=LendingLatestResponse,responses=ERRORS,
            operation_id='lending_latest_report')
def latest(id:UUID,service:Service,report_kind:ReportKind=ReportKind.LENDING_SHORT_SALES,
    trade_scope:Annotated[str,Query(max_length=100)]='unspecified',
    lending_channel:Annotated[str|None,Query(min_length=1,max_length=256)]=None,
    channel_family:Annotated[str|None,Query(min_length=1,max_length=256)]=None):
    """One latest series; ambiguous channels require explicit filters."""
    return service.latest(id,report_kind=report_kind,trade_scope=trade_scope,
        lending_channel=lending_channel,channel_family=channel_family)


@router.get('/lending/status',response_model=LendingStatusResponse,responses=ERRORS,
            operation_id='lending_ingestion_status')
def status(service:Service,market:Literal['TWSE','TPEX']|None=None,
    source_code:Annotated[str|None,Query(min_length=1,max_length=100)]=None,
    limit:Annotated[int,Query(ge=1,le=100)]=100,
    cursor:Annotated[str|None,Query(max_length=2048)]=None):
    """Saved source/kind/channel metadata, completeness and ingestion outcomes."""
    return service.status(market=market,source_code=source_code,limit=limit,cursor=cursor)


def install_error_handlers(app:FastAPI):
    @app.exception_handler(LendingQueryError)
    async def lending_error(request:Request,exc:LendingQueryError):
        code=exc.code if re.fullmatch(r'[a-z0-9_]{1,100}',exc.code) else 'lending_query_failed'
        message='Lending data is temporarily unavailable.' if exc.status_code==503 else exc.message
        return JSONResponse(status_code=exc.status_code,content=ErrorResponse(detail={
            'code':code,'message':message,'errors':None}).model_dump(mode='json'))
