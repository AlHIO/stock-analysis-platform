import asyncio
from stockapp.modules.health.domain.ports import DependencyProbe


async def readiness(probe: DependencyProbe) -> dict:
    async def check(method):
        try:
            return "ok" if await asyncio.wait_for(method(), timeout=3) else "error"
        except Exception:
            return "error"

    postgres, redis = await asyncio.gather(check(probe.postgres), check(probe.redis))
    return {
        "status": "ok" if postgres == redis == "ok" else "degraded",
        "dependencies": {"postgres": postgres, "redis": redis},
    }
