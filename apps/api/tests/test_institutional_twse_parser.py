"""Offline TWSE contracts using explicitly SYNTHETIC financial observations."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.models import Category, MappingStatus
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure.twse_parser import (
    LEGACY_CONTRACT, LEGACY_FIELDS, MODERN_CONTRACT, MODERN_FIELDS,
    TWSE_ENDPOINT, parse_twse,
)

FIXTURES = Path(__file__).parent / "fixtures/institutional"
SYNTHETIC_ID = UUID("00000000-0000-4000-8000-000000001301")
OBSERVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


def document(filename="twse_modern.json"):
    return json.loads((FIXTURES / filename).read_text())


def raw(payload=None, *, filename="twse_modern.json", **changes):
    payload = document(filename) if payload is None else payload
    values = dict(run_id=SYNTHETIC_ID, source_id=SYNTHETIC_ID, market_id=SYNTHETIC_ID,
        endpoint=TWSE_ENDPOINT, body=json.dumps(payload, ensure_ascii=False).encode(),
        content_type="application/json; charset=utf-8", status_code=200, fetched_at=OBSERVED_AT,
        request_parameters={"date": payload.get("date", "20261106"),
                            "selectType": "ALLBUT0999", "response": "json"})
    return RawPayload(**(values | changes))


def component(parsed, category):
    return next(flow for flow in parsed.rows[0].components if flow.category is category)


def test_modern_fixture_exact_values_are_shares_and_foreign_dealers_remain_separate():
    parsed = parse_twse(raw(), requested_date=date(2026, 11, 6))
    assert parsed.contract_version == MODERN_CONTRACT
    assert parsed.trade_date == date(2026, 11, 6) and parsed.parser_version == "1.0.0"
    assert parsed.quantity_unit.value == "shares" and parsed.scope.is_verified
    assert parsed.scope.includes_block_trades is True
    assert parsed.scope.includes_odd_lots is True and parsed.scope.includes_after_hours is True
    assert "ALLBUT0999" in parsed.scope.code
    assert parsed.issues == () and len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.row_index == 0 and row.locator == "data/0" and row.symbol == "SYNTH1"
    assert row.name == "SYNTHETIC TWSE modern"
    assert component(parsed, Category.FOREIGN_EX_DEALERS).buy_quantity == 1000
    assert component(parsed, Category.FOREIGN_EX_DEALERS).net_quantity == 600
    assert component(parsed, Category.FOREIGN_DEALERS).net_quantity == 150
    assert component(parsed, Category.INVESTMENT_TRUST).net_quantity == 200
    assert component(parsed, Category.DEALER_PROPRIETARY).net_quantity == 50
    assert component(parsed, Category.DEALER_HEDGING).net_quantity == -100
    subtotal = component(parsed, Category.DEALER_TOTAL)
    assert subtotal.net_quantity == -50 and subtotal.buy_quantity is subtotal.sell_quantity is None
    assert component(parsed, Category.THREE_MAJOR_TOTAL).net_quantity == 750
    assert len(MODERN_FIELDS) == 19


def test_legacy_fixture_never_claims_modern_foreign_classification_or_verified_scope():
    parsed = parse_twse(raw(filename="twse_legacy.json"), requested_date=date(2016, 11, 7))
    assert parsed.contract_version == LEGACY_CONTRACT and len(LEGACY_FIELDS) == 16
    foreign = component(parsed, Category.FOREIGN_UNSPECIFIED)
    assert (foreign.buy_quantity, foreign.sell_quantity, foreign.net_quantity) == (1000, 400, 600)
    assert foreign.mapping_status is MappingStatus.UNVERIFIED
    assert Category.FOREIGN_EX_DEALERS not in {flow.category for flow in parsed.rows[0].components}
    assert Category.FOREIGN_DEALERS not in {flow.category for flow in parsed.rows[0].components}
    assert not parsed.scope.is_verified and parsed.scope.includes_block_trades is None
    assert parsed.scope.basis.value == "unspecified"


def test_permuted_known_columns_keep_their_financial_meaning():
    payload = document()
    order = tuple(reversed(range(len(payload["fields"]))))
    payload["fields"] = [payload["fields"][index] for index in order]
    payload["data"] = [[row[index] for index in order] for row in payload["data"]]
    assert parse_twse(raw(payload)).rows[0].components == parse_twse(raw()).rows[0].components


@pytest.mark.parametrize("change", ["missing", "extra", "rename"])
def test_unknown_or_missing_columns_do_not_use_position_fallback(change):
    payload = document()
    if change == "missing":
        payload["fields"].pop()
        payload["data"][0].pop()
    elif change == "extra":
        payload["fields"].append("SYNTHETIC unknown quantity")
        payload["data"][0].append("0")
    else:
        payload["fields"][2] = "SYNTHETIC renamed foreign quantity"
    with pytest.raises(InstitutionalSourceError, match="unknown_schema"):
        parse_twse(raw(payload))


@pytest.mark.parametrize("duplicate", ["證券代號", " 證券代號 "])
def test_duplicate_columns_and_whitespace_alias_collisions_are_rejected(duplicate):
    payload = document()
    payload["fields"].append(duplicate)
    payload["data"][0].append("SYNTH2")
    with pytest.raises(InstitutionalSourceError, match="duplicate_columns"):
        parse_twse(raw(payload))


@pytest.mark.parametrize("filename,day", [
    ("twse_modern.json", "20161107"), ("twse_legacy.json", "20261106"),
    ("twse_modern.json", "20251106"), ("twse_legacy.json", "20161108"),
])
def test_known_schema_is_not_extrapolated_to_unverified_dates(filename, day):
    payload = document(filename)
    payload["date"] = day
    with pytest.raises(InstitutionalSourceError, match="unverified_schema_date"):
        parse_twse(raw(payload))


@pytest.mark.parametrize("column,value", [
    ("外陸資買賣超股數(不含外資自營商)", "601"),
    ("自營商買賣超股數(自行買賣)", "51"),
    ("自營商買進股數(避險)", "-1"),
    ("投信買進股數", 1.5),
    ("投信買進股數", True),
    ("投信買進股數", "1,23"),
    ("投信買進股數", "NaN"),
    ("投信買進股數", str(2**63)),
])
def test_bad_individual_quantity_retains_other_rows_and_an_auditable_issue(column, value):
    payload = document()
    bad = deepcopy(payload["data"][0])
    bad[0] = "SYNTH_BAD"
    bad[payload["fields"].index(column)] = value
    payload["data"].append(bad)
    payload["total"] = 2
    parsed = parse_twse(raw(payload))
    assert [row.symbol for row in parsed.rows] == ["SYNTH1"]
    assert len(parsed.issues) == 1
    issue = parsed.issues[0]
    assert issue.row_index == 1 and issue.locator == "data/1" and issue.symbol == "SYNTH_BAD"
    assert issue.raw_row[column] == value and issue.code


def test_zero_and_missing_values_are_not_collapsed_or_invented():
    payload = document()
    for column, value in (("投信買進股數", "0"), ("投信賣出股數", "--"), ("投信買賣超股數", None)):
        payload["data"][0][payload["fields"].index(column)] = value
    trust = component(parse_twse(raw(payload)), Category.INVESTMENT_TRUST)
    assert trust.buy_quantity == 0
    assert trust.sell_quantity is None and trust.net_quantity is None and trust.resolved_net is None


@pytest.mark.parametrize("values", [[], {}, ["SYNTH_BAD"]])
def test_bad_row_width_is_quarantined_without_dropping_valid_rows(values):
    payload = document()
    payload["data"].append(values)
    payload["total"] = 2
    parsed = parse_twse(raw(payload))
    assert len(parsed.rows) == 1 and parsed.issues[0].code == "row_width_mismatch"
    assert parsed.issues[0].raw_row == {"values": values}


@pytest.mark.parametrize("field,value", [
    ("stat", "SYNTHETIC maintenance"), ("fields", None), ("data", {}),
    ("data", []), ("total", 100), ("selectType", "ALL"), ("date", "1151106"),
])
def test_unexpected_envelope_and_scope_are_rejected(field, value):
    payload = document()
    payload[field] = value
    with pytest.raises(InstitutionalSourceError):
        parse_twse(raw(payload))


def test_requested_date_must_match_source_and_raw_request_date():
    with pytest.raises(InstitutionalSourceError, match="date_mismatch"):
        parse_twse(raw(), requested_date=date(2026, 11, 5))
    wrong = replace(raw(), request_parameters={"date": "20261105", "selectType": "ALLBUT0999", "response": "json"})
    with pytest.raises(InstitutionalSourceError, match="date_mismatch"):
        parse_twse(wrong)
    with pytest.raises(InstitutionalSourceError, match="invalid_requested_date"):
        parse_twse(raw(), requested_date=OBSERVED_AT)


def test_source_trade_date_cannot_be_later_than_observation_local_date():
    with pytest.raises(InstitutionalSourceError, match="future_trade_date"):
        parse_twse(raw(fetched_at=datetime(2026, 11, 5, tzinfo=timezone.utc)))


def test_duplicate_json_keys_cannot_be_silently_overwritten():
    body = raw().body.decode().replace('"stat": "OK"', '"stat": "OK", "stat": "OK"')
    with pytest.raises(InstitutionalSourceError, match="duplicate_columns"):
        parse_twse(raw(body=body.encode()))


def test_wrong_endpoint_and_non_json_http_response_never_parse_as_source_data():
    with pytest.raises(InstitutionalSourceError):
        parse_twse(raw(endpoint="https://example.invalid/synthetic/other"))
    with pytest.raises(InstitutionalSourceError):
        parse_twse(raw(body=b"<html>SYNTHETIC blocked page</html>", content_type="text/html"))
    with pytest.raises(InstitutionalSourceError):
        parse_twse(raw(status_code=403))
