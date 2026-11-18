"""SYNTHETIC H5 contracts, exact numbers and bounded read-only service behavior."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from stockapp.modules.broker_flow.api.dependencies import get_broker_query_service
from stockapp.modules.broker_flow.api.routes import router, install_error_handlers
from stockapp.modules.broker_flow.api.schemas import BrokerLatestResponse
from stockapp.modules.broker_flow.application.query_service import BrokerQueryService
from stockapp.modules.broker_flow.domain.analytics import BrokerAnalysisHistory, CalendarSnapshot
from stockapp.modules.broker_flow.domain.query import BrokerQueryError
from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
from test_broker_flow_domain import INSTRUMENT, MARKET, SOURCE, SEEN
from test_broker_flow_flow_calculations import DAY, synthetic_input

NOW = SEEN + timedelta(days=2)
BRANCH = UUID(int=1)


class SyntheticReader:
    def __init__(self, rows=None, *, verified_calendar=False):
        self.rows = (synthetic_input(),) if rows is None else tuple(rows)
        self.verified_calendar = verified_calendar
        self.load_calls = []

    def instrument(self, identifier):
        if identifier != INSTRUMENT:
            return None
        return dict(id=INSTRUMENT, market=dict(id=MARKET, code="TWSE", name="SYNTHETIC market",
            timezone="Asia/Taipei", currency="TWD"), symbol="SYNTHETIC_H5", name="SYNTHETIC security",
            instrument_type="stock", status="active", quote_currency="TWD", quantity_unit="shares",
            created_at=SEEN, updated_at=SEEN)

    def source(self, identifier):
        return dict(id=identifier, code="SYNTHETIC_H5", name="SYNTHETIC source")

    def _selected(self, selection, broker=None, branch=None):
        return tuple(item for item in self.rows
            if (selection.trade_scope is None or item.report.scope.id == selection.trade_scope)
            and (selection.source is None or item.report.lineage.source_id == selection.source)
            and (branch is None or any(trade.branch_id == branch for trade in item.trades))
            and (broker is None or any(identity.broker_id == broker for identity in item.identities)))

    def scopes(self, identifier, start, end, *, selection):
        return tuple({item.report.scope.id: dict(id=item.report.scope.id, source_id=item.report.lineage.source_id)
                      for item in self._selected(selection) if start <= item.trade_date <= end}.values())

    def page(self, identifier, start, end, *, selection, after=None, order="asc", limit=250, broker=None, branch=None):
        key = lambda item: (item.trade_date, item.report.scope.id)
        rows = tuple(sorted((item for item in self._selected(selection, broker, branch)
            if start <= item.trade_date <= end and (after is None or
            (key(item) > after if order == "asc" else key(item) < after))), key=key, reverse=order == "desc"))
        return rows[:limit]

    def latest(self, identifier, *, selection, broker=None, branch=None):
        return max(self._selected(selection, broker, branch), key=lambda item: item.trade_date, default=None)

    def extent(self, identifier, *, selection, broker=None, branch=None):
        days = tuple(item.trade_date for item in self._selected(selection, broker, branch))
        return min(days, default=None), max(days, default=None), len(days)

    def load(self, request, *, selection):
        self.load_calls.append((request, selection))
        rows = tuple(item for item in self._selected(selection) if request.start <= item.trade_date <= request.end)
        # Explicit synthetic delivery-calendar evidence, never production inference.
        calendar = CalendarSnapshot(market_id=MARKET, start=DAY, end=DAY + timedelta(days=4),
            open_dates=(DAY, DAY + timedelta(days=3), DAY + timedelta(days=4)), verified=True,
            observed_at=SEEN, version="SYNTHETIC-H5-calendar-v1", evidence_reference="SYNTHETIC only") if self.verified_calendar else None
        return BrokerAnalysisHistory(reports=rows, calendar=calendar)

    def status(self, *, limit=100, source=None):
        return ()


def service(rows=None, **kwargs):
    return BrokerQueryService(SyntheticReader(rows, **kwargs), now=lambda: NOW)


def client(query=None):
    app = FastAPI()
    app.include_router(router)
    common_errors(app)
    install_error_handlers(app)
    app.dependency_overrides[get_broker_query_service] = lambda: query or service()
    return TestClient(app)


def path(suffix=""):
    return f"/api/v1/stocks/{INSTRUMENT}/broker-flow{suffix}"


def dates():
    return {"from": DAY.isoformat(), "to": DAY.isoformat()}


def test_empty_existing_stock_unknown_stock_and_no_imaginary_data():
    with client(service(())) as http:
        result = http.get(path(), params=dates())
        assert result.status_code == 200, result.text
        assert result.json()["items"] == [] and result.json()["freshness"]["assessment"] == "no_data"
        assert http.get(path("/latest")).json()["point"] is None
        assert http.get(f"/api/v1/stocks/{uuid4()}/broker-flow").status_code == 404
        assert http.get("/api/v1/broker-flow/status").json()["items"] == []


def test_report_exact_values_identity_revision_null_publication_and_permissions():
    with client() as http:
        result = http.get(path("/latest"))
        assert result.status_code == 200, result.text
        body = result.json()
        point = body["point"]
        first, second = point["trades"][:2]
        assert (first["buy_quantity"], first["sell_quantity"], first["net_quantity"]) == ("1000", "100", "900")
        assert second["net_quantity"] == "-600" and first["net_amount"] == "18000"
        assert first["identity"]["branch_code"] == "SYNTHETIC-1"
        assert point["lineage"]["revision_id"] == str(synthetic_input().revision_id)
        assert point["timing"]["source_published_at"] is None and point["quality"]["provisional"]
        assert point["calculations"][0]["algorithm_id"] == "broker.branch_net_quantity"
        assert point["calculations"][0]["algorithm_version"] == "1.0.0"
        assert point["calculations"][0]["formula"] == "daily net_quantity = reported buy_quantity - reported sell_quantity"
        assert not body["usage"]["automated_download_enabled"] and not body["usage"]["public_display_verified"]


def test_history_filter_keeps_original_universe_count_and_no_relabelled_daily_data():
    with client() as http:
        body = http.get(path(), params=dates() | {"branch": str(BRANCH)}).json()
        point = body["items"][0]
        assert point["filtered"] and point["total_branch_count"] == 8 and len(point["trades"]) == 1
        assert body["frequency"] == "1d" and body["coverage"]["expected_dates_known"] is False


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_cursor_binds_filters_order_and_keeps_same_date_independent_scopes(order):
    item = synthetic_input()
    other = replace(item, report=replace(item.report, scope=replace(item.report.scope, id=uuid4())))
    query = service((item, other))
    arguments = dict(start=DAY, end=DAY, order=order, limit=1)
    first = query.history(INSTRUMENT, **arguments)
    second = query.history(INSTRUMENT, **arguments, cursor=first["next_cursor"])
    assert first["items"][0]["scope"]["id"] != second["items"][0]["scope"]["id"]
    assert second["next_cursor"] is None
    with pytest.raises(BrokerQueryError, match="Cursor"):
        query.history(INSTRUMENT, **(arguments | {"branch": BRANCH}), cursor=first["next_cursor"])
    with pytest.raises(BrokerQueryError) as error:
        query.latest(INSTRUMENT)
    assert error.value.code == "ambiguous_trade_scope"


@pytest.mark.parametrize("suffix,params", [
    ("", {"from": "20260102"}), ("", {"from": "2026-02-30"}),
    ("", {"from": "2026-01-04", "to": "2026-01-02"}),
    ("", {"to": "2099-01-01"}), ("", {"from": "2000-01-01"}),
    ("", {"limit": "251"}), ("", {"limit": "0"}), ("", {"timeframe": "1w"}),
    ("", {"trade_scope": "regular"}), ("", {"branch": "2330"}),
    ("", {"cursor": "malformed"}), ("/top-branches", {"top_n": "7"}),
    ("/top-branches", {"side": "investors"}), ("/metrics", {"algorithm": "evil.sql"}),
    ("/metrics", {"query_as_of": "2026-01-02T10:00:00"}),
    ("/metrics", {"query_as_of": "2099-01-02T10:00:00Z"}),
    ("/metrics", {"algorithm_parameters": "[]"}),
    ("/metrics", {"algorithm_parameters": '{"top_n":5,"top_n":10}'}),
    ("/metrics", {"algorithm_parameters": '{"top_n":NaN}'}),
    ("/metrics", {"algorithm_parameters": "{" + "x" * 4096 + "}"}),
    ("/metrics", {"algorithm": "broker.top_branches", "algorithm_parameters": '{"top_n":true}'}),
    ("/metrics", {"algorithm": "broker.source_coverage", "algorithm_parameters": '{"calendar_verified":true}'}),
])
def test_invalid_http_inputs_are_422(suffix, params):
    with client() as http:
        response = http.get(path(suffix), params=dates() | params)
        assert response.status_code == 422, response.text


def test_metrics_call_domain_engine_and_do_not_invent_calendar_or_prices():
    query = service()
    with client(query) as http:
        result = http.get(path("/metrics"), params=dates() | {"algorithm": "broker.gross_turnover_hhi"})
        assert result.status_code == 200, result.text
        metric = result.json()["result"]
        assert metric["value"] == "0.234714003945" and metric["algorithm_version"] == "1.0.0"
        assert metric["input_refs"][0]["revision_id"] == str(synthetic_input().revision_id)
        unknown = http.get(path("/top-branches"), params={"from": DAY.isoformat(), "to": "2026-01-06"}).json()["result"]
        assert unknown["status"] == "not_verified" and unknown["value"] is None
        price = http.get(path("/metrics"), params=dates() | {"algorithm": "broker.flow_price_alignment", "branch": str(BRANCH)})
        assert price.status_code == 200, price.text
        assert price.json()["result"]["status"] == "unavailable"
        unsupported = http.get(path("/metrics"), params=dates() | {"algorithm": "investor.medium_large_net_flow"}).json()["result"]
        assert unsupported["status"] == "unsupported" and unsupported["algorithm_version"] is None
    assert len(query.repository.load_calls) == 4


def test_top_filter_does_not_shrink_denominator_ranking_or_input_revisions():
    with client() as http:
        result = http.get(path("/top-branches"), params=dates() | {"branch": str(UUID(int=3)), "top_n": "5"})
        assert result.status_code == 200, result.text
        metric = result.json()["result"]
        assert len(metric["value"]) == 1 and metric["value"][0]["rank"] == 2
        assert metric["details"]["output_filter"]["ranking_universe_filtered"] is False
        assert len(metric["input_refs"]) == 1


def test_exact_json_threshold_and_conflicting_branch_filters():
    with client() as http:
        response = http.get(path("/metrics"), params=dates() | {
            "algorithm": "proxy.broker_daily_gross_amount_threshold_net",
            "algorithm_parameters": '{"threshold_amount":20000.000000000000,"currency":"TWD"}'})
        assert response.status_code == 200, response.text
        assert response.json()["result"]["value"] == "400"
        assert response.json()["result"]["parameters"]["threshold_amount"] == "20000.000000000000"
        conflict = http.get(path("/metrics"), params=dates() | {"algorithm": "broker.branch_buy_streak", "branch": str(BRANCH),
            "algorithm_parameters": json.dumps({"branch_id": str(UUID(int=2))})})
        assert conflict.status_code == 422


def test_dependency_unavailable_and_invalid_stored_response_are_safe_503():
    app = FastAPI()
    app.include_router(router)
    common_errors(app)
    install_error_handlers(app)
    with TestClient(app) as http:
        result = http.get(path("/latest"))
        assert result.status_code == 503 and "postgres" not in result.text.lower()
    class Broken:
        def latest(self, *args, **kwargs):
            raise BrokerQueryError("database_failed", "postgresql://secret:secret@private sql SELECT password", 503)
    with client(Broken()) as http:
        result = http.get(path("/latest"))
        assert result.status_code == 503 and "secret" not in result.text and "SELECT" not in result.text


def test_response_rejects_float_or_extra_unreviewed_fields():
    body = service().latest(INSTRUMENT)
    body["point"]["trades"][0]["buy_amount"] = 1.5
    with pytest.raises(ValueError):
        BrokerLatestResponse.model_validate(body)


def test_huge_quantity_remains_json_string_and_decimal_exponent_is_not_lost():
    item = synthetic_input(values=((2**63-1, 0),), money=False)
    with client(service((item,))) as http:
        result = http.get(path("/latest"))
        assert result.status_code == 200, result.text
        trade = result.json()["point"]["trades"][0]
        assert trade["buy_quantity"] == "9223372036854775807" and trade["buy_amount"] is None
    small = synthetic_input(values=((1, 0),))
    small = replace(small, report=replace(small.report, trades=(replace(small.trades[0], buy_amount=Decimal("1e-12")),)))
    with client(service((small,))) as http:
        result = http.get(path("/latest"))
        assert result.status_code == 200, result.text
        assert result.json()["point"]["trades"][0]["buy_amount"] == "0.000000000001"


def test_status_retains_real_ingestion_counter_names_without_error_or_secret_text():
    from stockapp.modules.broker_flow.infrastructure.query_repository import SqlAlchemyBrokerQueryRepository
    from stockapp.modules.market_data.domain.runtime import RuntimeRun
    run = RuntimeRun(id=uuid4(), source_id=SOURCE, status="failed", started_at=SEEN,
        finished_at=NOW, error_summary="postgresql://private:password@secret/db",
        stats={"accepted_reports": 2, "partial_reports": 1, "rejected_rows": 3,
               "source_failures": 1, "rejected_reports": -1, "api_token": 123})
    value = SqlAlchemyBrokerQueryRepository._run(run)
    assert value["counts"] == {"accepted_reports": 2, "partial_reports": 1, "rejected_rows": 3, "source_failures": 1}
    assert value["safe_failure_code"] == "ingestion_failed"
    assert "private" not in json.dumps(value, default=str) and "api_token" not in value["counts"]


def test_openapi_exposes_all_broker_routes_exact_financial_types_and_query_limits():
    from stockapp.main import create_app
    schema = create_app().openapi()
    base = "/api/v1/stocks/{id}/broker-flow"
    assert {base, base + "/latest", base + "/top-branches", base + "/metrics", "/api/v1/broker-flow/status"} <= schema["paths"].keys()
    components = schema["components"]["schemas"]
    assert components["BrokerTopN"]["enum"] == [5, 10, 15, 30]
    assert len(components["BrokerAlgorithm"]["enum"]) == 24
    assert components["BrokerTradeSchema"]["properties"]["buy_quantity"]["anyOf"][0]["type"] == "string"
    assert components["BrokerTradeSchema"]["properties"]["buy_amount"]["anyOf"][0]["type"] == "string"
    parameters = {value["name"]: value for value in schema["paths"][base]["get"]["parameters"]}
    assert parameters["limit"]["schema"]["maximum"] == 250
    assert parameters["trade_scope"]["schema"]["anyOf"][0]["format"] == "uuid"
    assert {"404", "422", "503"} <= schema["paths"][base + "/metrics"]["get"]["responses"].keys()
