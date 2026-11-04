"""HTTP contract tests with explicitly synthetic data and no provider or DB I/O."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from stockapp.modules.market_data.api import dependencies
from stockapp.modules.market_data.api.dependencies import get_query_service
from stockapp.modules.market_data.api.routes import install_error_handlers, router
from stockapp.modules.market_data.api.schemas import BarSchema, BarsResponse, FreshnessSchema
from stockapp.modules.market_data.domain.models import InstrumentType
from stockapp.modules.market_data.domain.queries import InvalidQuery, InvalidStoredData, ResourceNotFound


INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000001")
MARKET_ID = UUID("00000000-0000-0000-0000-000000000002")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000003")
RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
UNKNOWN_ID = UUID("00000000-0000-0000-0000-000000000099")
NOW = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
INGESTED = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def synthetic_market():
    return {"id": MARKET_ID, "code": "TEST", "name": "Synthetic test market", "timezone": "Asia/Taipei", "currency": "TWD"}


def synthetic_source():
    return {"id": SOURCE_ID, "code": "TEST", "name": "Synthetic test source", "source_type": "manual"}


def synthetic_instrument():
    return {
        "id": INSTRUMENT_ID, "market": synthetic_market(), "symbol": "TEST123", "name": "測試股票",
        "instrument_type": "stock", "status": "active", "created_at": INGESTED, "updated_at": INGESTED,
        "quote_currency": "TWD", "quantity_unit": "shares",
    }


def synthetic_bar():
    return {
        "trade_date": date(2026, 11, 2), "period_start": date(2026, 11, 2), "period_end": date(2026, 11, 2),
        "first_trade_date": date(2026, 11, 2), "last_trade_date": date(2026, 11, 2),
        "open": Decimal("100.00000001"), "high": Decimal("101.00000002"), "low": Decimal("99.00000001"),
        "close": Decimal("100.12345678"), "volume_shares": 1234, "turnover": Decimal("123456789012345678901.12345678"),
        "trade_count": None, "observed_session_count": 1, "expected_session_count": None,
        "period_closed": True, "completeness": "unknown", "is_complete": False,
        "completeness_reasons": ["calendar_not_verified"], "missing_trade_dates": [],
        "calendar_missing_dates": [date(2026, 11, 2)], "sources": [synthetic_source()], "last_ingested_at": INGESTED,
        "previous_observed": {"trade_date": date(2026, 10, 30), "close": Decimal("99.12345678")},
        "previous_session_verified": False, "reference_price": None, "change": None, "change_percent": None,
        "change_unavailable_reason": "official_reference_not_available",
    }


def synthetic_freshness(empty=False):
    return {
        "as_of": NOW, "market_timezone": "Asia/Taipei", "market_local_date": date(2026, 11, 4),
        "latest_trade_date": None if empty else date(2026, 11, 2),
        "last_ingested_at": None if empty else INGESTED, "age_calendar_days": None if empty else 2,
        "is_realtime": False, "assessment": "no_data" if empty else "calendar_not_verified",
    }


def synthetic_history(empty=False):
    return {
        "available_from": None if empty else date(2026, 11, 2),
        "available_to": None if empty else date(2026, 11, 2), "completeness": "not_verified",
    }


class FakeQueryService:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.empty = False

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.failure:
            raise self.failure
        if args and args[0] != INSTRUMENT_ID:
            raise ResourceNotFound("instrument_not_found", "Instrument was not found.")

    def search(self, **kwargs):
        self._record("search", **kwargs)
        return {"items": [] if self.empty else [synthetic_instrument()], "total": 0 if self.empty else 1,
                "page": kwargs["page"], "page_size": kwargs["page_size"], "total_pages": 0 if self.empty else 1}

    def instrument(self, instrument_id):
        self._record("instrument", instrument_id)
        return synthetic_instrument()

    def bars(self, instrument_id, **kwargs):
        self._record("bars", instrument_id, **kwargs)
        return {"instrument": synthetic_instrument(), "timeframe": kwargs["timeframe"],
                "from": kwargs["start"] or date(2026, 11, 1), "to": kwargs["end"] or date(2026, 11, 4),
                "count": 0 if self.empty else 1, "bars": [] if self.empty else [synthetic_bar()],
                "freshness": synthetic_freshness(self.empty), "history": synthetic_history(self.empty),
                "price_adjustment": "unadjusted", "data_kind": "daily_snapshot", "query_limit": kwargs["limit"]}

    def latest(self, instrument_id):
        self._record("latest", instrument_id)
        return {"instrument": synthetic_instrument(), "bar": None if self.empty else synthetic_bar(),
                "freshness": synthetic_freshness(self.empty), "history": synthetic_history(self.empty),
                "price_adjustment": "unadjusted", "data_kind": "daily_snapshot"}

    def status(self, **kwargs):
        self._record("status", **kwargs)
        run = {"id": RUN_ID, "status": "failed", "started_at": INGESTED, "finished_at": INGESTED,
               "stats": {"accepted": 1, "rejected": 2}, "has_partial_data": True, "failure_code": "partial_import"}
        return {"as_of": NOW, "is_realtime": False, "latest_trade_date": date(2026, 11, 2), "bar_count": 1,
                "history_completeness": "not_verified", "markets": [{"market": synthetic_market(),
                    "latest_trade_date": date(2026, 11, 2), "instrument_count": 1, "bar_count": 1}],
                "sources": [{"source": synthetic_source(), "market_codes": ["TEST"],
                    "latest_trade_date": date(2026, 11, 2), "last_ingested_at": INGESTED, "bar_count": 1,
                    "latest_run": run, "latest_sync_run": deepcopy(run), "latest_successful_sync_at": None}],
                "total_markets": 1, "total_sources": 1, "truncated": False}


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    service = FakeQueryService()
    app.dependency_overrides[get_query_service] = lambda: service
    with TestClient(app) as client:
        yield client, service


def test_search_filters_pagination_and_unicode_response(api):
    client, service = api
    response = client.get("/api/v1/instruments", params={"q": "測試", "symbol": "TEST", "name": "股票", "market": "TEST", "page": 2, "page_size": 10})
    assert response.status_code == 200
    assert service.calls == [("search", (), {"q": "測試", "symbol": "TEST", "name": "股票", "market": "TEST", "instrument_type": None, "page": 2, "page_size": 10})]
    assert response.json()["items"][0]["name"] == "測試股票"
    assert response.json()["items"][0]["market"]["timezone"] == "Asia/Taipei"
    assert response.json()["page"] == 2


def test_search_defaults_and_empty_result(api):
    client, service = api
    service.empty = True
    response = client.get("/api/v1/instruments")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 50, "total_pages": 0}
    assert service.calls[0][2] == {"q": None, "symbol": None, "name": None, "market": None, "instrument_type": None, "page": 1, "page_size": 50}


@pytest.mark.parametrize("instrument_type", list(InstrumentType))
def test_search_accepts_supported_security_type(api, instrument_type):
    client, service = api
    response = client.get("/api/v1/instruments", params={"instrument_type": instrument_type.value})
    assert response.status_code == 200
    assert service.calls[0][2]["instrument_type"] is instrument_type


def test_get_instrument_uses_uuid_identity(api):
    client, service = api
    response = client.get(f"/api/v1/instruments/{INSTRUMENT_ID}")
    assert response.status_code == 200
    assert response.json()["id"] == str(INSTRUMENT_ID)
    assert response.json()["symbol"] == "TEST123"
    assert service.calls == [("instrument", (INSTRUMENT_ID,), {})]


@pytest.mark.parametrize("timeframe", ["1d", "1w", "1M"])
def test_bars_dates_aliases_and_exact_financial_serialization(api, timeframe):
    client, service = api
    response = client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/bars", params={"from": "2026-10-30", "to": "2026-11-03", "timeframe": timeframe, "limit": 40})
    assert response.status_code == 200
    body = response.json()
    assert service.calls == [("bars", (INSTRUMENT_ID,), {"timeframe": timeframe, "start": date(2026, 10, 30), "end": date(2026, 11, 3), "limit": 40})]
    assert body["from"] == "2026-10-30" and body["to"] == "2026-11-03"
    assert "from_" not in body
    bar = body["bars"][0]
    assert bar["close"] == "100.12345678"
    assert bar["turnover"] == "123456789012345678901.12345678"
    assert bar["previous_observed"] == {"trade_date": "2026-10-30", "close": "99.12345678"}
    assert isinstance(bar["volume_shares"], int)
    assert bar["sources"][0]["code"] == "TEST"
    assert bar["change"] is None and bar["change_percent"] is None and bar["reference_price"] is None
    assert body["freshness"]["is_realtime"] is False
    assert body["history"]["completeness"] == "not_verified"
    assert body["price_adjustment"] == "unadjusted" and body["data_kind"] == "daily_snapshot"


def test_bars_default_arguments(api):
    client, service = api
    assert client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/bars").status_code == 200
    assert service.calls == [("bars", (INSTRUMENT_ID,), {"timeframe": "1d", "start": None, "end": None, "limit": 1000})]


def test_empty_bars_and_latest_are_successful_with_explicit_no_data(api):
    client, service = api
    service.empty = True
    bars = client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/bars")
    latest = client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/latest")
    assert bars.status_code == latest.status_code == 200
    assert bars.json()["bars"] == [] and bars.json()["count"] == 0
    assert latest.json()["bar"] is None
    assert latest.json()["freshness"]["assessment"] == "no_data"
    assert latest.json()["freshness"]["latest_trade_date"] is None
    assert latest.json()["history"] == {"available_from": None, "available_to": None, "completeness": "not_verified"}


def test_latest_and_data_status_show_stored_data_and_safe_run_summary(api):
    client, service = api
    latest = client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/latest")
    assert latest.status_code == 200
    assert latest.json()["bar"]["trade_date"] == "2026-11-02"
    assert latest.json()["freshness"]["last_ingested_at"] == "2026-11-02T10:00:00Z"
    status = client.get("/api/v1/data/status", params={"limit": 25})
    assert status.status_code == 200
    assert service.calls[-1] == ("status", (), {"limit": 25})
    body = status.json()
    assert body["is_realtime"] is False and body["history_completeness"] == "not_verified"
    assert body["sources"][0]["latest_run"]["failure_code"] == "partial_import"
    assert body["sources"][0]["latest_run"]["stats"] == {"accepted": 1, "rejected": 2}
    assert "error_summary" not in status.text


@pytest.mark.parametrize("path", ["instruments/{id}", "stocks/{id}/bars", "stocks/{id}/latest"])
def test_unknown_instrument_is_404(api, path):
    client, _ = api
    response = client.get("/api/v1/" + path.format(id=UNKNOWN_ID))
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "instrument_not_found", "message": "Instrument was not found.", "errors": None}}


@pytest.mark.parametrize("path,params", [
    ("instruments", {"page": 0}), ("instruments", {"page": 1001}),
    ("instruments", {"page_size": 0}), ("instruments", {"page_size": 101}),
    ("instruments", {"q": "x" * 101}), ("instruments", {"symbol": "x" * 33}),
    ("instruments", {"name": "x" * 201}), ("instruments", {"market": "x" * 33}),
    ("instruments", {"instrument_type": "unknown"}), ("instruments", {"instrument_type": "STOCK"}),
    ("instruments", {"instrument_type": ""}),
    (f"stocks/{INSTRUMENT_ID}/bars", {"timeframe": "1m"}),
    (f"stocks/{INSTRUMENT_ID}/bars", {"from": "2026-02-30"}),
    (f"stocks/{INSTRUMENT_ID}/bars", {"to": "invalid-date"}),
    (f"stocks/{INSTRUMENT_ID}/bars", {"limit": 0}),
    (f"stocks/{INSTRUMENT_ID}/bars", {"limit": 1001}),
    ("data/status", {"limit": 0}), ("data/status", {"limit": 101}),
    ("instruments/not-a-uuid", {}), ("stocks/not-a-uuid/bars", {}), ("stocks/not-a-uuid/latest", {}),
])
def test_http_validation_rejects_parameters_before_service_call(api, path, params):
    client, service = api
    response = client.get("/api/v1/" + path, params=params)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert all(set(issue) == {"loc", "message", "type"} for issue in response.json()["detail"]["errors"])
    assert service.calls == []


def test_validation_error_does_not_echo_sensitive_input(api):
    client, _ = api
    secret = "private-connection-credential-" * 10
    response = client.get("/api/v1/instruments", params={"q": secret})
    assert response.status_code == 422
    assert secret not in response.text and "input" not in response.text


def test_application_invalid_range_and_result_limit_are_422(api):
    client, service = api
    for code in ["invalid_date_range", "query_limit_exceeded"]:
        service.failure = InvalidQuery(code, "The requested query exceeds its allowed range.")
        response = client.get(f"/api/v1/stocks/{INSTRUMENT_ID}/bars", params={"from": "2026-11-04", "to": "2026-11-01"})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == code


@pytest.mark.parametrize("failure,code", [
    (OperationalError("SELECT secret", {}, RuntimeError("postgresql://user:password@private-host/db")), "database_unavailable"),
    (InvalidStoredData("internal_failure", "credentials: sensitive-stored-payload"), "invalid_stored_data"),
])
def test_database_and_corrupt_data_errors_are_sanitized_503(api, failure, code):
    client, service = api
    service.failure = failure
    response = client.get("/api/v1/data/status")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code
    assert "password" not in response.text and "sensitive" not in response.text and "private-host" not in response.text
    assert "SELECT" not in response.text


def test_openapi_exposes_response_contract_decimal_strings_and_date_alias(api):
    client, _ = api
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert set(paths) == {"/api/v1/instruments", "/api/v1/instruments/{id}", "/api/v1/stocks/{id}/bars", "/api/v1/stocks/{id}/latest", "/api/v1/data/status"}
    bars_operation = paths["/api/v1/stocks/{id}/bars"]["get"]
    assert {p["name"] for p in bars_operation["parameters"]} == {"id", "timeframe", "from", "to", "limit"}
    assert {"200", "404", "422", "503"}.issubset(bars_operation["responses"])
    bar = schema["components"]["schemas"]["BarSchema"]
    assert bar["properties"]["close"]["type"] == "string"
    assert bar["properties"]["volume_shares"]["type"] == "integer"
    assert "from" in schema["components"]["schemas"]["BarsResponse"]["properties"]
    error_ref = bars_operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert error_ref.endswith("/ErrorResponse")


def test_response_models_accept_aliases_and_preserve_decimals():
    bar = BarSchema.model_validate(synthetic_bar())
    assert bar.close == Decimal("100.12345678")
    assert bar.model_dump(mode="json")["close"] == "100.12345678"
    payload = FakeQueryService().bars(INSTRUMENT_ID, timeframe="1d", start=None, end=None, limit=1000)
    assert BarsResponse.model_validate(payload).model_dump(mode="json", by_alias=True)["from"] == "2026-11-01"
    payload["from_"] = payload.pop("from")
    assert BarsResponse.model_validate(payload).from_ == date(2026, 11, 1)


def test_response_timestamp_requires_timezone():
    payload = synthetic_freshness()
    payload["as_of"] = datetime(2026, 11, 4)
    with pytest.raises(ValueError, match="timezone"):
        FreshnessSchema.model_validate(payload)


def test_dependency_uses_read_only_transaction_and_bounded_database_timeouts(monkeypatch):
    commands = []
    engine = object()

    class FakeSession:
        def __init__(self, actual_engine):
            assert actual_engine is engine

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self):
            return self

        def execute(self, statement):
            commands.append(str(statement))

    monkeypatch.setattr(dependencies, "Session", FakeSession)
    monkeypatch.setattr(dependencies, "SqlAlchemyMarketQueryRepository", lambda session: session)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(market_query_engine=engine)))
    generator = get_query_service(request)
    assert next(generator) is not None
    with pytest.raises(StopIteration):
        next(generator)
    assert commands == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '10s'", "SET LOCAL lock_timeout = '3s'"]
