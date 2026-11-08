"""Read-only ownership query Ports; no framework or persistence dependencies."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import OwnershipWeeklyReport
from .query_results import WeeklyScheduleSnapshot

MAX_QUERY_DAYS=3660
MAX_QUERY_REPORTS=1000


class OwnershipQueryError(Exception):
    def __init__(self,code,message,status_code=422):
        self.code,self.message,self.status_code=code,message,status_code
        super().__init__(message)


class OwnershipReadRepository(Protocol):
    def instrument(self,instrument_id:UUID)->dict|None: ...
    def source(self,source_id:UUID)->dict: ...
    def page(self,instrument_id:UUID,start:date,end:date,*,after:date|None,order:str,limit:int)->tuple[OwnershipWeeklyReport,...]: ...
    def latest(self,instrument_id:UUID)->OwnershipWeeklyReport|None: ...
    def previous(self,instrument_id:UUID,before:date)->OwnershipWeeklyReport|None: ...
    def extent(self,instrument_id:UUID)->tuple[date|None,date|None,int]: ...
    def schedule(self,start:date,end:date,observed_dates:tuple[date,...])->WeeklyScheduleSnapshot: ...
    def status(self)->dict: ...
