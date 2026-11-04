from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from stockapp.config import Settings
from stockapp.modules.health.domain.ports import DependencyProbe
from stockapp.modules.health.application.service import readiness
from stockapp.modules.health.infrastructure.probe import InfrastructureProbe

router = APIRouter(tags=["health"])


def get_probe() -> DependencyProbe:
    return InfrastructureProbe(Settings())


@router.get("/health")
async def health():
    return {"status": "ok", "service": "stockapp-api"}


@router.get("/health/ready", responses={503: {"description": "Dependencies unavailable"}})
async def ready(probe: DependencyProbe = Depends(get_probe)):
    result = await readiness(probe)
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)
