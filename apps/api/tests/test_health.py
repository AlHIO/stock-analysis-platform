import pytest
from fastapi.testclient import TestClient
from stockapp.main import create_app
from stockapp.modules.health.api.routes import get_probe


class Probe:
    def __init__(self, postgres=True, redis=True, raises=False):
        self.pg, self.cache, self.raises = postgres, redis, raises

    async def postgres(self):
        if self.raises:
            raise ConnectionError("sensitive connection detail")
        return self.pg

    async def redis(self):
        return self.cache


def test_liveness_does_not_require_dependencies():
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "stockapp-api"}


@pytest.mark.parametrize("pg,cache,raises", [(True, True, False), (False, True, False), (True, False, False), (False, False, False), (True, True, True)])
def test_readiness(pg, cache, raises):
    app = create_app()
    app.dependency_overrides[get_probe] = lambda: Probe(pg, cache, raises)
    with TestClient(app) as client:
        response = client.get("/health/ready")
    healthy = pg and cache and not raises
    assert response.status_code == (200 if healthy else 503)
    assert response.json() == {
        "status": "ok" if healthy else "degraded",
        "dependencies": {"postgres": "ok" if pg and not raises else "error", "redis": "ok" if cache else "error"},
    }
    assert "sensitive" not in response.text
