"""SYNTHETIC numerical/query regressions without databases or provider I/O."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import UUID

import pytest

from test_institutional_models import synthetic_report, synthetic_scope, flow, MARKET, SOURCE
from stockapp.modules.institutional.application.query_service import InstitutionalQueryService
from stockapp.modules.institutional.domain.models import CalendarSnapshot, Category, QuantityUnit
from stockapp.modules.institutional.domain.queries import InstitutionalQueryError

TODAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 8, 0, tzinfo=timezone.utc)
START = date(2026, 1, 1)


def report(day, value=1, *, kind=None):
    original = synthetic_report()
    components = tuple(flow(category, max(value, 0), max(-value, 0), value) for category in (
        Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST,
        Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING))
    result = replace(original, trade_date=day, components=components)
    if kind == "partial":
        result = replace(result, components=(components[0], components[1], flow(Category.DEALER_TOTAL, net=value * 2)))
    return result


class MemoryQueries:
    def __init__(self, reports=(), *, verified=True, sessions=None):
        self.reports = tuple(reports)
        self.verified = verified
        self.sessions = tuple(sessions) if sessions is not None else tuple(sorted({row.trade_date for row in reports}))
        self.stock = {"id":synthetic_report().instrument_id,"symbol":"SYNTH1","name":"Synthetic stock",
            "instrument_type":"stock","status":"active","created_at":NOW,"updated_at":NOW,
            "quote_currency":"TWD","quantity_unit":"shares",
            "market":{"id":MARKET,"code":"TWSE","name":"Synthetic market","timezone":"Asia/Taipei","currency":"TWD"}}
    def instrument(self, instrument_id):
        return self.stock if instrument_id == self.stock["id"] else None
    def scopes(self, instrument_id):
        return tuple(dict.fromkeys(row.scope for row in self.reports))
    def page(self, instrument_id, scope, start, end, *, after, order, limit):
        rows = [row for row in self.reports if row.scope == scope and start <= row.trade_date <= end
            and (after is None or row.trade_date > after if order == "asc" else after is None or row.trade_date < after)]
        return tuple(sorted(rows, key=lambda row:row.trade_date, reverse=order == "desc")[:limit])
    def latest(self, instrument_id, scope):
        return max((row for row in self.reports if row.scope == scope), key=lambda row:row.trade_date, default=None)
    def extent(self, instrument_id, scope):
        dates = [row.trade_date for row in self.reports if row.scope == scope]
        return (min(dates),max(dates)) if dates else (None,None)
    def source(self, source_id):
        return dict(id=source_id, code="SYNTH_SOURCE", name="Synthetic source")
    def calendar(self, market_id, start, end, observed_dates):
        return CalendarSnapshot(market_id=market_id, start=start, end=end,
            session_dates=tuple(day for day in self.sessions if start <= day <= end),
            verified=self.verified, version="synthetic-confirmed-sessions-v1")
    def status_rows(self, *, market, source_code, after, limit):
        return [{"key":key} for key in ("TPEX:SYNTH", "TWSE:SYNTH") if after is None or key > after][:limit]


def service(reports=(), **kwargs):
    repo = MemoryQueries(reports, **kwargs)
    return InstitutionalQueryService(repo, now=lambda:NOW), repo


def test_source_bigints_and_daily_totals_are_exact_and_not_double_counted():
    day = START
    big = 2**63 - 1
    app, repo = service((report(day, big),))
    result = app.history(repo.stock["id"], start=day, end=day)
    point = result["items"][0]
    assert point["components"][0]["buy_quantity"] == str(big)
    derived = {value["category"]: value for value in point["derived_daily"]}
    assert derived[Category.DEALER_TOTAL]["value"] == str(big * 2)
    assert derived[Category.THREE_MAJOR_TOTAL]["value"] == str(big * 4)
    assert point["quality"]["status"] == "verified"
    assert point["provenance"]["source"]["id"] == SOURCE
    assert point["timing"]["as_of_date"] == day and point["timing"]["source_published_at"] is None


def test_all_four_confirmed_session_windows_use_domain_values():
    days = tuple(START + timedelta(days=index * 2) for index in range(60))
    app, repo = service(tuple(report(day, index + 1) for index,day in enumerate(days)))
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1])
    values = {point["period"]: point["value"] for point in result["cumulative"]}
    assert values == {period:str(sum(range(61-period,61))) for period in (5,10,20,60)}
    assert result["streak"]["status"] == "truncated_history"
    assert result["streak"]["buy_count"] is None and result["streak"]["buy_lower_bound"] == 60


@pytest.mark.parametrize("category,factor", [(Category.FOREIGN_EX_DEALERS,1), (Category.INVESTMENT_TRUST,1),
    (Category.DEALER_PROPRIETARY,1),(Category.DEALER_HEDGING,1),(Category.DEALER_TOTAL,2),(Category.THREE_MAJOR_TOTAL,4)])
def test_category_calculations_share_domain_formula(category, factor):
    days = tuple(START + timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day, 3) for day in days))
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1], category=category, period=5)
    assert result["cumulative"][0]["value"] == str(15 * factor)


def test_missing_expected_session_does_not_become_shorter_window_or_streak():
    days = tuple(START + timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day, 2) for day in days if day != days[3]), sessions=days)
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1], period=5)
    assert result["cumulative"][0]["status"] == result["streak"]["status"] == "missing_data"
    assert result["cumulative"][0]["value"] is None
    assert result["cumulative"][0]["missing_dates"] == (days[3],)
    assert result["coverage"]["known_missing_dates"] == [days[3]]
    assert result["streak"]["buy_count"] is None and result["streak"]["buy_lower_bound"] == 1


def test_unverified_calendar_never_claims_numeric_window_or_streak():
    days = tuple(START + timedelta(days=index) for index in range(60))
    app,repo = service(tuple(report(day,1) for day in days), verified=False)
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1])
    assert all(point["value"] is None and point["status"] == "not_verified" for point in result["cumulative"])
    assert result["streak"]["status"] == "not_verified" and result["streak"]["buy_count"] is None
    assert result["coverage"]["expected_dates_known"] is False


def test_confirmed_non_session_asof_anchors_to_actual_previous_session():
    days = tuple(START + timedelta(days=index * 2) for index in range(5))
    app,repo = service(tuple(report(day,1) for day in days))
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1]+timedelta(days=1), period=5)
    assert result["cumulative"][0]["value"] == "5"
    assert result["cumulative"][0]["anchor_date"] == days[-1]
    assert result["daily"]["as_of"] == days[-1]


def test_zero_is_real_zero_and_stops_streak():
    days = tuple(START + timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day,0) for day in days))
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1], period=5)
    assert result["cumulative"][0]["value"] == "0"
    assert result["streak"]["buy_count"] == result["streak"]["sell_count"] == 0


def test_sell_streak_stops_at_confirmed_opposite_direction():
    days = tuple(START + timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day,1 if index == 0 else -2) for index,day in enumerate(days)))
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1])
    assert result["streak"]["sell_count"] == 4 and result["streak"]["buy_count"] == 0


def test_partial_dealer_totals_are_not_used_to_fill_missing_leafs():
    days = tuple(START + timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day,2,kind="partial") for day in days))
    point = app.latest(repo.stock["id"])["point"]
    assert point["quality"]["status"] == "partial"
    assert next(flow for flow in point["components"] if flow["category"] == "dealer_proprietary")["net_quantity"] is None
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1], category=Category.THREE_MAJOR_TOTAL, period=5)
    assert result["cumulative"][0]["value"] is None and result["cumulative"][0]["status"] == "missing_data"
    assert result["daily"]["value"] is None


@pytest.mark.parametrize("field,value", [("source_id",UUID(int=999)),("source_contract_version","synthetic-v2")])
def test_incompatible_source_profiles_do_not_merge(field,value):
    days = tuple(START + timedelta(days=index) for index in range(5))
    rows = [report(day,1) for day in days]
    rows[-2] = replace(rows[-2], lineage=replace(rows[-2].lineage, **{field:value}))
    app,repo = service(rows)
    result = app.metrics(repo.stock["id"], start=days[0], end=days[-1],period=5)
    assert result["cumulative"][0]["status"] == "incompatible_data" and result["cumulative"][0]["value"] is None
    assert result["streak"]["status"] == "incompatible_data"


@pytest.mark.parametrize("order", ["asc","desc"])
def test_keyset_pagination_and_filter_signature(order):
    days = tuple(START+timedelta(days=index) for index in range(5))
    app,repo = service(tuple(report(day) for day in days))
    args = dict(start=days[0],end=days[-1],limit=2,order=order)
    first = app.history(repo.stock["id"], **args)
    second = app.history(repo.stock["id"], **args, cursor=first["next_cursor"])
    third = app.history(repo.stock["id"], **args, cursor=second["next_cursor"])
    assert [point["trade_date"] for page in (first,second,third) for point in page["items"]] == sorted(days,reverse=order=="desc")
    assert third["next_cursor"] is None
    with pytest.raises(InstitutionalQueryError, match="Cursor"):
        app.history(repo.stock["id"], **(args|{"limit":3}), cursor=first["next_cursor"])


def test_full_scope_json_resolves_same_code_different_flags_without_mixing():
    first = report(START)
    second = replace(first, scope=replace(first.scope, includes_block_trades=not first.scope.includes_block_trades))
    app,repo = service((first,second))
    with pytest.raises(InstitutionalQueryError) as error:
        app.history(repo.stock["id"], start=START,end=START,trade_scope=first.scope.code)
    assert error.value.code == "ambiguous_trade_scope"
    result = app.history(repo.stock["id"], start=START,end=START,trade_scope=json.dumps(asdict(first.scope)))
    assert result["count"] == 1 and result["items"][0]["trade_scope"]["includes_block_trades"] == first.scope.includes_block_trades


def test_existing_empty_stock_has_null_latest_and_no_fake_metrics_values():
    app,repo = service()
    assert app.history(repo.stock["id"])["items"] == []
    assert app.latest(repo.stock["id"])["point"] is None
    metrics = app.metrics(repo.stock["id"])
    assert metrics["daily"] is None and all(value["value"] is None for value in metrics["cumulative"])
    assert metrics["coverage"]["completeness"] == "not_verified"


@pytest.mark.parametrize("arguments", [{"start":TODAY,"end":START},{"end":TODAY+timedelta(days=1)},
    {"start":date(2000,1,1),"end":TODAY},{"limit":0},{"limit":True},{"cursor":"bad"},{"order":"random"}])
def test_invalid_history_parameters(arguments):
    app,repo = service((report(START),))
    with pytest.raises(InstitutionalQueryError) as error:
        app.history(repo.stock["id"],**arguments)
    assert error.value.status_code == 422


def test_missing_or_unsupported_instrument_returns_404():
    app,repo = service()
    with pytest.raises(InstitutionalQueryError) as error:
        app.latest(UUID(int=999))
    assert error.value.status_code == 404
    repo.stock["instrument_type"] = "etf"
    with pytest.raises(InstitutionalQueryError) as error:
        app.latest(repo.stock["id"])
    assert error.value.status_code == 404


def test_status_cursor_is_bound_to_its_filter():
    app,repo = service()
    first = app.status(limit=1)
    second = app.status(limit=1,cursor=first["next_cursor"])
    assert [first["items"][0]["key"],second["items"][0]["key"]] == ["TPEX:SYNTH","TWSE:SYNTH"]
    with pytest.raises(InstitutionalQueryError):
        app.status(limit=1,market="TPEX",cursor=first["next_cursor"])


def test_metric_inputs_over_1000_reports_are_rejected_before_calculation():
    days = tuple(date(2023, 1, 1) + timedelta(days=index) for index in range(1001))
    app, repo = service(tuple(report(day) for day in days))
    with pytest.raises(InstitutionalQueryError) as error:
        app.metrics(repo.stock["id"], start=days[0], end=days[-1])
    assert error.value.code == "query_limit_exceeded"
