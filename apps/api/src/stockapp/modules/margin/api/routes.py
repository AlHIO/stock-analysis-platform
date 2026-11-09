"""Canonical margin REST reads; no GET operation downloads provider data."""
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from stockapp.modules.market_data.api.schemas import ErrorResponse
from stockapp.modules.margin.application.query_service import MarginQueryService
from stockapp.modules.margin.domain.queries import MarginQueryError
from .dependencies import get_margin_query_service
from .schemas import MarginHistoryResponse, MarginLatestResponse, MarginStatusResponse, QueryDate

router=APIRouter(prefix='/api/v1',tags=['Margin trading'])
Service=Annotated[MarginQueryService,Depends(get_margin_query_service)]
ERRORS={
    404:{'model':ErrorResponse,'description':'Instrument missing or unsupported'},
    422:{'model':ErrorResponse,'description':'Invalid or excessive margin query'},
    503:{'model':ErrorResponse,'description':'Margin data temporarily unavailable'},
}


@router.get('/stocks/{id}/margin',response_model=MarginHistoryResponse,responses=ERRORS,
            operation_id='margin_daily_history')
def margin_history(id:UUID,service:Service,
    start:Annotated[QueryDate|None,Query(alias='from')]=None,
    end:Annotated[QueryDate|None,Query(alias='to')]=None,
    balance_scope:Annotated[str,Query(max_length=64)]='margin_trading',
    limit:Annotated[int,Query(ge=1,le=1000)]=250,
    cursor:Annotated[str|None,Query(max_length=2048)]=None,
    order:Annotated[Literal['asc','desc'],Query()]='asc',
) -> dict:
    """Return whole canonical reports, separate stock balances and daily flows."""
    return service.history(id,start=start,end=end,balance_scope=balance_scope,limit=limit,cursor=cursor,order=order)


@router.get('/stocks/{id}/margin/latest',response_model=MarginLatestResponse,responses=ERRORS,
            operation_id='margin_latest_report')
def margin_latest(id:UUID,service:Service,
    balance_scope:Annotated[str,Query(max_length=64)]='margin_trading',
) -> dict:
    """Latest saved report, never a realtime quote; point=null when absent."""
    return service.latest(id,balance_scope=balance_scope)


@router.get('/margin/status',response_model=MarginStatusResponse,responses=ERRORS,
            operation_id='margin_ingestion_status')
def margin_status(service:Service,
    market:Annotated[Literal['TWSE','TPEX']|None,Query()]=None,
    source_code:Annotated[str|None,Query(min_length=1,max_length=100)]=None,
    limit:Annotated[int,Query(ge=1,le=100)]=100,
    cursor:Annotated[str|None,Query(max_length=2048)]=None,
) -> dict:
    """Saved ingestion outcomes and source capability gates; no provider I/O."""
    return service.status(market=market,source_code=source_code,limit=limit,cursor=cursor)


def install_error_handlers(app:FastAPI) -> None:
    @app.exception_handler(MarginQueryError)
    async def margin_query_error(request:Request,exc:MarginQueryError):
        code=exc.code if re.fullmatch(r'[a-z0-9_]{1,100}',exc.code) else 'margin_query_failed'
        message='Margin data is temporarily unavailable.' if exc.status_code==503 else exc.message
        return JSONResponse(status_code=exc.status_code,content=ErrorResponse(detail={
            'code':code,'message':message,'errors':None}).model_dump(mode='json'))
