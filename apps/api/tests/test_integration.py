import os
import pytest
from fastapi.testclient import TestClient
from stockapp.main import create_app


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL and Redis running")
def test_real_dependencies():
    with TestClient(create_app()) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"] == {"postgres": "ok", "redis": "ok"}
