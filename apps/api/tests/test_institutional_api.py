"""SYNTHETIC HTTP contracts: exact values, validation and safe failures, no I/O."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest
from sqlalchemy.exc import OperationalError

from test_institutional_query_service import START, report, service
from stockapp.main import create_app
from stockapp.modules.institutional.api.dependencies import get_institutional_service
from stockapp.modules.institutional.api.routes import install_error_handlers, router
from stockapp.modules.institutional.api.schemas import DerivedQuantity, SourceQuantity, FactTimingSchema
from stockapp.modules.institutional.domain.queries import InstitutionalQueryError
from stockapp.modules.market_data.api.routes import install_error_handlers as install_market_errors


@pytest.fixture
def api():
    days = tuple(START + timedelta(days=index) for index in range(60))
    app_service, repository = service(tuple(report(day, index + 1) for index, day in enumerate(days)))
    repository.status_rows = lambda **kwargs: []
    app = FastAPI()
    app.include_router(router)
    install_market_errors(app)
    install_error_handlers(app)
    app.dependency_overrides[get_institutional_service] = lambda: app_service
    with TestClient(app) as client:
        yield client, app_service, repository, days


def path(repository, suffix=""):
    return f"/api/v1/stocks/{repository.stock['id']}/institutional{suffix}"


def test_real_service_history_serializes_canonical_lineage_and_dates(api):
    client, _, repository, days = api
    response = client.get(path(repository), params={"from": str(days[0]), "to": str(days[-1]), "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["from"] == str(days[0]) and body["count"] == 2 and body["next_cursor"]
    point = body["items"][0]
    assert point["components"][0]["net_quantity"] == "1"
    assert point["derived_daily"][-1]["value"] == "4"
    assert point["timing"]["timezone_name"] == "Asia/Taipei"
    assert point["provenance"]["source"]["code"] == "SYNTH_SOURCE"
    assert body["freshness"]["is_realtime"] is False
    latest = client.get(path(repository, "/latest"))
    assert latest.status_code == 200
    assert latest.json()["point"]["trade_date"] == str(days[-1])


@pytest.mark.parametrize("period,expected", [(5,"290"),(10,"555"),(20,"1010"),(60,"1830")])
def test_metric_query_period_and_exact_domain_numbers(api, period, expected):
    client, _, repository, days = api
    response = client.get(path(repository, "/metrics"), params={"from":str(days[0]), "to":str(days[-1]), "period":period})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["cumulative"]) == 1 and body["cumulative"][0]["value"] == expected
    assert body["algorithm_version"] == "1.0.0" and body["precision"]["internal_digits"] == 50
    assert body["streak"]["buy_count"] is None and body["streak"]["buy_lower_bound"] == 60
    assert body["streak"]["status"] == "truncated_history"


def test_empty_stock_responses_and_status(api):
    client, _, repository, _ = api
    repository.reports = ()
    assert client.get(path(repository)).json()["items"] == []
    assert client.get(path(repository, "/latest")).json()["point"] is None
    metrics = client.get(path(repository, "/metrics"))
    assert metrics.status_code == 200 and metrics.json()["daily"] is None
    assert all(point["value"] is None for point in metrics.json()["cumulative"])
    status = client.get("/api/v1/chip-data/status", params={"product":"institutional_daily"})
    assert status.status_code == 200 and status.json()["items"] == []


@pytest.mark.parametrize("suffix,params", [
    ("",{"from":"20260101"}), ("",{"from":"2026-01-01T00:00:00Z"}),
    ("",{"from":"2026-02-30"}), ("",{"from":"2026-02-01","to":"2026-01-01"}),
    ("",{"limit":0}), ("",{"limit":1001}), ("",{"order":"random"}),
    ("",{"cursor":"malformed"}), ("",{"trade_scope":"{"}),
    ("/metrics",{"period":7}), ("/metrics",{"category":"foreign_total"}),
])
def test_invalid_inputs_are_422_without_echoing_input(api, suffix, params):
    client, _, repository, _ = api
    response = client.get(path(repository, suffix), params=params)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] in {"invalid_request", "invalid_date_range", "invalid_cursor", "invalid_trade_scope", "invalid_metric_parameters"}


@pytest.mark.parametrize("suffix", ["", "/latest", "/metrics"])
def test_missing_stock_is_404(api, suffix):
    client, _, _, _ = api
    response = client.get(f"/api/v1/stocks/{UUID(int=999)}/institutional{suffix}")
    assert response.status_code == 404 and response.json()["detail"]["code"] == "instrument_not_found"


@pytest.mark.parametrize("params", [{"product":"ownership"},{"market":"US"},{"limit":101},{"cursor":"bad"}])
def test_status_invalid_parameters(api, params):
    client, _, _, _ = api
    assert client.get("/api/v1/chip-data/status", params=params).status_code == 422


@pytest.mark.parametrize("failure,code", [
    (OperationalError("SELECT secret", {}, RuntimeError("postgresql://private:password@host/db")), "database_unavailable"),
    (InstitutionalQueryError("invalid_stored_chip_data", "private password", 503), "invalid_stored_chip_data"),
])
def test_dependency_failures_are_safe_503(api, failure, code):
    client, _, repository, _ = api
    def unavailable():
        raise failure
    client.app.dependency_overrides[get_institutional_service] = unavailable
    response = client.get(path(repository))
    assert response.status_code == 503 and response.json()["detail"]["code"] == code
    assert "password" not in response.text and "SELECT" not in response.text and "postgresql" not in response.text


def test_invalid_stored_response_is_safe_503(api):
    client, app_service, repository, _ = api
    app_service.latest = lambda *args, **kwargs: {"secret":"private password SQL"}
    response = client.get(path(repository, "/latest"))
    assert response.status_code == 503 and "private" not in response.text


@pytest.mark.parametrize("value", [1.1, Decimal("NaN"), "Infinity", "bad", "1.5", "1E51", True])
def test_derived_quantities_reject_float_noninteger_nonfinite_or_excess_precision(value):
    with pytest.raises(ValidationError):
        TypeAdapter(DerivedQuantity).validate_python(value)


def test_source_bigint_and_derived_precision_have_different_bounds():
    maximum = 2**63-1
    with pytest.raises(ValidationError):
        TypeAdapter(SourceQuantity).validate_python(str(maximum+1))
    adapter = TypeAdapter(DerivedQuantity)
    value = adapter.validate_python(str(maximum*60))
    assert adapter.dump_python(value, mode="json") == str(maximum*60)


def test_timestamp_requires_timezone_and_serializes_utc():
    aware = datetime(2026,1,2,8,tzinfo=timezone(timedelta(hours=8)))
    payload = dict(as_of_date=START, source_published_on=None, source_published_at=aware,
        first_seen_at=aware,last_seen_at=aware,ingested_at=aware,timezone_name="Asia/Taipei")
    assert FactTimingSchema.model_validate(payload).model_dump(mode="json")["source_published_at"] == "2026-01-02T00:00:00Z"
    with pytest.raises(ValidationError):
        FactTimingSchema.model_validate(payload | {"first_seen_at":aware.replace(tzinfo=None)})


def test_openapi_has_four_routes_exact_strings_and_errors():
    schema = create_app().openapi()
    base = "/api/v1/stocks/{id}/institutional"
    expected = {base,base+"/latest",base+"/metrics","/api/v1/chip-data/status"}
    assert expected <= schema["paths"].keys()
    for route in expected:
        operation = schema["paths"][route]["get"]
        assert {"200","404","422","503"} <= operation["responses"].keys()
    components = schema["components"]["schemas"]
    source = components["ComponentSchema"]["properties"]["net_quantity"]["anyOf"]
    derived = components["NetResultSchema"]["properties"]["value"]["anyOf"]
    assert {item["type"] for item in source} == {"string","null"}
    assert {item["type"] for item in derived} == {"string","null"}
