"""SYNTHETIC offline archive/serialization contracts; no database or provider."""
from dataclasses import replace
from datetime import timezone
from decimal import Decimal, Inexact, localcontext
from hashlib import sha256

import pytest

from stockapp.modules.broker_flow.domain.models import BrokerFlowError
from stockapp.modules.broker_flow.domain.persistence import RawPayload
from stockapp.modules.broker_flow.infrastructure.repositories import (
    _audit_context, report_from_dict, report_to_dict,
)
from test_broker_flow_domain import (
    RAW, RUN, SOURCE, SEEN, synthetic_report, synthetic_trade,
)


def synthetic_raw(**changes):
    return RawPayload(**(dict(id=RAW, run_id=RUN, source_id=SOURCE,
        source_contract_version="SYNTHETIC-contract-v1", endpoint="offline://synthetic/report.csv",
        body=b"SYNTHETIC,not-an-official-report\r\n", content_type="text/csv",
        status_code=200, fetched_at=SEEN) | changes))


def test_original_bytes_and_hash_are_preserved():
    raw = synthetic_raw(body=b"\xef\xbb\xbfSYNTHETIC\r\n")
    assert raw.body == b"\xef\xbb\xbfSYNTHETIC\r\n"
    assert raw.sha256 == sha256(raw.body).hexdigest()
    assert synthetic_raw(body=raw.body.replace(b"\r\n", b"\n")).sha256 != raw.sha256


@pytest.mark.parametrize("parameters", [
    {"token": "SYNTHETIC"}, {"nested": [{"Authorization": "SYNTHETIC"}]},
    {"password": "SYNTHETIC"}, {"api_key": "SYNTHETIC"},
    {"query": {"cookie": "SYNTHETIC"}}, {"date": float("nan")},
])
def test_sensitive_or_unrepresentable_request_metadata_is_rejected(parameters):
    with pytest.raises(BrokerFlowError):
        synthetic_raw(request_parameters=parameters)


@pytest.mark.parametrize("endpoint,method", [
    ("file:///tmp/report.csv", "FILE"),
    ("https://example.invalid/report?token=SYNTHETIC", "GET"),
    ("https://user:password@example.invalid/report", "GET"),
    ("http://example.invalid/report", "GET"),
    ("offline://synthetic/report#fragment", "FILE"),
    ("https://example.invalid/report", "FILE"),
])
def test_archive_locators_do_not_capture_credentials_or_local_paths(endpoint, method):
    with pytest.raises(BrokerFlowError):
        synthetic_raw(endpoint=endpoint, request_method=method)


def test_raw_request_metadata_is_detached_from_callers_mutable_input():
    parameters = {"SYNTHETIC": ["original"]}
    raw = synthetic_raw(request_parameters=parameters)
    parameters["SYNTHETIC"].append("changed")
    assert raw.request_parameters == {"SYNTHETIC": ["original"]}


def test_raw_replay_cannot_reference_itself_or_remove_observation_timezone():
    with pytest.raises(BrokerFlowError):
        synthetic_raw(replay_of_raw_id=RAW)
    with pytest.raises(BrokerFlowError):
        synthetic_raw(fetched_at=SEEN.replace(tzinfo=None))


def test_typed_snapshot_roundtrip_preserves_decimal_null_zero_and_all_timestamps():
    report = synthetic_report(currency="TWD", trades=(synthetic_trade(
        buy_quantity=3, sell_quantity=1,
        buy_amount=Decimal("0.300000030000"), sell_amount=Decimal("0.100000010000")),))
    encoded = report_to_dict(report)
    assert encoded["trades"][0]["buy_amount"] == "0.30000003"
    assert encoded["trades"][0]["sell_amount"] == "0.10000001"
    assert encoded["lineage"]["source_published_at"] is None
    assert report_from_dict(encoded) == report
    zero = replace(report, trades=(synthetic_trade(buy_quantity=0, sell_quantity=0),))
    restored = report_from_dict(report_to_dict(zero))
    assert restored.trades[0].net_quantity == 0
    assert restored.trades[0].buy_amount is None
    assert restored.lineage.first_seen_at.tzinfo == timezone.utc


@pytest.mark.parametrize("endpoint", [
    "https://[malformed/report.csv", "offline://[malformed/report.csv",
    "https://example.invalid:bad/report.csv", "https://example.invalid:65536/report.csv",
    "https://exam\nple.invalid/report.csv", "https://example.invalid/report\r.csv",
    "https://example.invalid/\treport.csv", " https://example.invalid/report.csv",
    "https://example.invalid/report with spaces.csv", "https://example.invalid/report\x00.csv",
    "https://example.invalid/report\x7f.csv", "https://example.invalid\\report.csv",
    "https://example.invalid/\ud800.csv",
])
def test_malformed_and_control_character_locators_have_stable_safe_errors(endpoint):
    method = "FILE" if endpoint.startswith("offline:") else "GET"
    with pytest.raises(BrokerFlowError, match="^invalid_raw_endpoint$"):
        synthetic_raw(endpoint=endpoint, request_method=method)


def test_recursive_request_parameters_fail_as_a_domain_error():
    parameters = {}
    parameters["SYNTHETIC-recursive"] = parameters
    with pytest.raises(BrokerFlowError, match="^invalid_raw_request_parameters$"):
        synthetic_raw(request_parameters=parameters)


def test_recursive_audit_context_fails_as_a_safe_domain_error():
    context = {"SYNTHETIC": []}
    context["SYNTHETIC"].append(context)
    with pytest.raises(BrokerFlowError, match="^invalid_audit_json$"):
        _audit_context(context)


@pytest.mark.parametrize("field,maximum", [
    ("source_contract_version", 256), ("part_key", 256), ("content_type", 256),
])
def test_raw_text_bounds_match_the_database_and_reject_unsupported_unicode(field, maximum):
    assert getattr(synthetic_raw(**{field: "x" * maximum}), field) == "x" * maximum
    for value in ("x" * (maximum + 1), "\ud800"):
        with pytest.raises(BrokerFlowError):
            synthetic_raw(**{field: value})


def test_full_precision_amount_roundtrip_ignores_ambient_decimal_context():
    amount = Decimal("99999999999999999999999999.999999999999")
    report = synthetic_report(currency="TWD", trades=(synthetic_trade(
        buy_amount=amount, sell_amount=Decimal("0.000000000001")),))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        encoded = report_to_dict(report)
        restored = report_from_dict(encoded)
    assert encoded["trades"][0]["buy_amount"] == str(amount)
    assert restored == report
