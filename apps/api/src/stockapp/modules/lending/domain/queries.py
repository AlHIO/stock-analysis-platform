"""Bounded canonical lending read ports; no transport or storage dependencies."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import LendingDailyReport, ReportKind

DEFAULT_SCOPE='unspecified'
MAX_QUERY_DAYS=3660
MAX_QUERY_REPORTS=1000
ChannelKey=tuple[str,str]
PageKey=tuple[date,str,str]


class LendingQueryError(Exception):
    def __init__(self,code:str,message:str,status_code:int=422):
        self.code,self.message,self.status_code=code,message,status_code
        super().__init__(message)


class LendingReadRepository(Protocol):
    def instrument(self,instrument_id:UUID)->dict|None: ...
    def source(self,source_id:UUID)->dict: ...
    def observation_timing(self,report:LendingDailyReport)->dict: ...
    def page(self,instrument_id:UUID,start:date,end:date,*,report_kind:ReportKind,scope:str,
             channel:str|None,family:str|None,after:PageKey|None,order:str,limit:int)->tuple[LendingDailyReport,...]: ...
    def channels(self,instrument_id:UUID,*,report_kind:ReportKind,scope:str,
                 channel:str|None,family:str|None)->tuple[ChannelKey,...]: ...
    def latest(self,instrument_id:UUID,*,report_kind:ReportKind,scope:str,
               channel:str|None,family:str|None)->LendingDailyReport|None: ...
    def extent(self,instrument_id:UUID,*,report_kind:ReportKind,scope:str,
               channel:str|None,family:str|None)->tuple[date|None,date|None,int]: ...
    def status(self)->tuple[dict,...]: ...
