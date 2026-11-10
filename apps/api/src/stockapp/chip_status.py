"""Composition of versioned dataset status, using one read-only DB snapshot."""
from typing import Annotated,Any,Literal

from fastapi import APIRouter,Depends,Query,Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.institutional.api.schemas import InstitutionalStatusResponse
from stockapp.modules.institutional.public import institutional_query_service
from stockapp.modules.market_data.api.schemas import ErrorResponse
from stockapp.modules.ownership.api.schemas import OwnershipStatusResponse
from stockapp.modules.ownership.public import ownership_query_service
from stockapp.modules.margin.api.schemas import MarginStatusResponse
from stockapp.modules.margin.public import margin_query_service
from stockapp.modules.lending.api.schemas import LendingStatusResponse
from stockapp.modules.lending.public import lending_query_service

Product=Literal['institutional_daily','ownership_weekly','margin_daily','lending_daily']
router=APIRouter(tags=['Chip data status'])


def get_chip_status_service(request:Request,product:Annotated[Product,Query()]='institutional_daily'):
    with Session(request.app.state.market_query_engine) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='10s'"))
        session.execute(text("SET LOCAL lock_timeout='3s'"))
        yield (ownership_query_service(session) if product=='ownership_weekly'
               else margin_query_service(session) if product=='margin_daily'
               else lending_query_service(session) if product=='lending_daily'
               else institutional_query_service(session))


@router.get('/chip-data/status',response_model=InstitutionalStatusResponse|OwnershipStatusResponse|MarginStatusResponse|LendingStatusResponse,
    operation_id='institutional_source_status',responses={
        404:{'model':ErrorResponse},422:{'model':ErrorResponse},503:{'model':ErrorResponse}})
def chip_data_status(service:Annotated[Any,Depends(get_chip_status_service)],
    product:Annotated[Product,Query()]='institutional_daily',
    market:Annotated[Literal['TWSE','TPEX']|None,Query()]=None,
    source_code:Annotated[str|None,Query(max_length=100)]=None,
    limit:Annotated[int,Query(ge=1,le=100)]=100,
    cursor:Annotated[str|None,Query(max_length=2048)]=None):
    """Read source/run metadata; ownership is global and rejects market filters."""
    return service.status(product=product,market=market,source_code=source_code,limit=limit,cursor=cursor)
