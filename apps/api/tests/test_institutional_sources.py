"""SYNTHETIC source contract tests; never download or persist market data."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure.parsing import decode_json, parse_date, parse_quantity
from stockapp.modules.institutional.infrastructure.providers import TpexInstitutionalAdapter, TwseInstitutionalAdapter
from stockapp.modules.institutional.infrastructure.source_manifest import MANIFESTS, MANIFEST_VERSION
from stockapp.modules.institutional.infrastructure.transport import InstitutionalHttpTransport

FIXTURES = Path(__file__).parent / "fixtures/institutional"
RUN = UUID(int=301)
SOURCE = UUID(int=302)
MARKET = UUID(int=303)
FETCHED = datetime(2026, 11, 7, 3, tzinfo=timezone.utc)


def synthetic_raw(name="twse_modern"):
    key = "tpex_openapi" if name == "tpex_openapi" else "twse_t86"
    parameters = {} if key == "tpex_openapi" else {
        "date": "20161107" if name == "twse_legacy" else "20261106",
        "selectType": "ALLBUT0999", "response": "json"}
    return RawPayload(run_id=RUN, source_id=SOURCE, market_id=MARKET,
        endpoint=MANIFESTS[key].endpoint, request_parameters=parameters,
        body=(FIXTURES / f"{name}.json").read_bytes(), content_type="application/json; charset=utf-8",
        status_code=200, fetched_at=FETCHED)


@pytest.mark.parametrize("value,format,expected", [
    ("20261106", "gregorian_compact", date(2026, 11, 6)),
    ("1151106", "roc_compact", date(2026, 11, 6)),
    ("2026/11/06", "gregorian_slash", date(2026, 11, 6)),
    ("115/11/06", "roc_slash", date(2026, 11, 6)),
    ("1130229", "roc_compact", date(2024, 2, 29)),
    ("0010101", "roc_compact", date(1912, 1, 1)),
])
def test_explicit_calendar_conversion(value, format, expected):
    assert parse_date(value, format) == expected


@pytest.mark.parametrize("value,format", [
    ("1150229", "roc_compact"), ("0000101", "roc_compact"),
    ("20260229", "gregorian_compact"), ("20261301", "gregorian_compact"),
    ("2026-11-06", "gregorian_compact"), (20261106, "gregorian_compact"),
    (None, "roc_compact"), ("20261106", "auto"),
])
def test_invalid_or_ambiguous_dates_are_not_guessed(value, format):
    with pytest.raises(InstitutionalSourceError):
        parse_date(value, format)


@pytest.mark.parametrize("value,expected", [("1,000", 1000), ("-1,000", -1000),
    ("+25", 25), ("0", 0), (0, 0), (None, None), ("--", None), ("  ", None),
    (str(2**63 - 1), 2**63 - 1), (str(-(2**63)), -(2**63))])
def test_counts_preserve_exact_shares_and_missing_values(value, expected):
    assert parse_quantity(value) == expected


@pytest.mark.parametrize("value", [True, 1.25, "1.0", "1e3", "12,34", "NaN", "-", "9" * 5000,
    str(2**63), str(-(2**63) - 1)])
def test_invalid_wire_counts_are_rejected(value):
    with pytest.raises(InstitutionalSourceError, match="invalid_quantity"):
        parse_quantity(value)


@pytest.mark.parametrize("body,code", [(b'{"Date":"1151106","Date":"1141106"}', "duplicate_columns"),
    (b'{"value":NaN}', "invalid_json_number"), (b"<html>CAPTCHA</html>", "unexpected_response"),
    (b'{"value":' + b"9" * 5000 + b"}", "unexpected_response"), (b"\xff", "unexpected_response")])
def test_decoder_rejects_ambiguous_or_unexpected_responses(body, code):
    raw = replace(synthetic_raw(), body=body)
    with pytest.raises(InstitutionalSourceError, match=code):
        decode_json(raw, expected_endpoint=raw.endpoint)


def test_manifest_versions_capabilities_and_schema_fingerprints():
    assert MANIFEST_VERSION == "institutional-sources-1.0.0"
    twse, tpex = MANIFESTS["twse_t86"], MANIFESTS["tpex_openapi"]
    assert not twse.default_network_enabled and not tpex.default_network_enabled
    assert twse.authorization == "pending" and twse.license is None
    assert tpex.license == "OGDL-1.0" and tpex.capability == "latest_only"
    assert tpex.contracts[0].verified_historical_dates == ()
    assert {day for contract in twse.contracts for day in contract.verified_historical_dates} == {
        date(2016, 11, 7), date(2026, 11, 6)}
    assert [len(contract.fields) for contract in twse.contracts] == [16, 19]
    assert len(tpex.contracts[0].fields) == 20
    for manifest in MANIFESTS.values():
        assert manifest.source_quantity_unit == "shares" and manifest.unit_factor == 1
        assert manifest.published_at is None and manifest.official_rate_limit is None
        for contract in manifest.contracts:
            assert len(contract.schema_sha256) == 64
            assert replace(contract, fields=tuple(reversed(contract.fields))).schema_sha256 == contract.schema_sha256


def test_network_defaults_and_pending_rights_make_no_requests():
    class NoNetwork:
        async def fetch(self, **kwargs):
            pytest.fail("disabled adapter must never invoke transport")

    async def scenario():
        for cls, code in [(TwseInstitutionalAdapter, "source_authorization_pending"),
                          (TpexInstitutionalAdapter, "network_disabled")]:
            async with cls(transport=NoNetwork()) as adapter:
                with pytest.raises(InstitutionalSourceError, match=code):
                    await adapter.fetch(run_id=RUN, source_id=SOURCE, market_id=MARKET)
        with pytest.raises(InstitutionalSourceError, match="source_authorization_pending"):
            TwseInstitutionalAdapter(network_enabled=True, transport=NoNetwork())
    asyncio.run(scenario())


def test_tpex_explicit_opt_in_uses_only_latest_official_endpoint_with_mock_http():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=synthetic_raw("tpex_openapi").body,
            headers={"Content-Type": "application/json"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            transport = InstitutionalHttpTransport(client=client, now=lambda: FETCHED)
            async with TpexInstitutionalAdapter(network_enabled=True, transport=transport) as adapter:
                with pytest.raises(InstitutionalSourceError, match="historical_query_unsupported"):
                    await adapter.fetch(run_id=RUN, source_id=SOURCE, market_id=MARKET, trade_date=date(2016, 11, 7))
                assert not requests
                raw = await adapter.fetch(run_id=RUN, source_id=SOURCE, market_id=MARKET)
                batch = adapter.parse(raw)
                assert batch.trade_date == date(2026, 11, 6)
                assert batch.raw_id == raw.id and batch.raw_sha256 == raw.sha256
                assert raw.run_id == RUN and raw.source_id == SOURCE
                assert requests[0].url == MANIFESTS["tpex_openapi"].endpoint
                assert not requests[0].url.query
    asyncio.run(scenario())


@pytest.mark.parametrize("name,cls", [("twse_modern", TwseInstitutionalAdapter),
    ("twse_legacy", TwseInstitutionalAdapter), ("tpex_openapi", TpexInstitutionalAdapter)])
def test_offline_parsing_works_while_network_is_disabled(name, cls):
    raw = synthetic_raw(name)
    batch = cls().parse(raw)
    assert len(batch.rows) == 1 and not batch.issues
    assert batch.raw_id == raw.id and batch.raw_sha256 == raw.sha256


def test_parser_error_retains_complete_raw_for_lineage():
    raw = replace(synthetic_raw(), body=b"not JSON")
    with pytest.raises(InstitutionalSourceError) as error:
        TwseInstitutionalAdapter().parse(raw)
    assert error.value.raw is raw


def test_overlong_quantity_isolated_to_its_row_instead_of_aborting_batch():
    raw = synthetic_raw()
    payload = json.loads(raw.body)
    good = payload["data"][0]
    bad = [*good]
    bad[0], bad[2] = "SYNTH_BAD", "9" * 5000
    payload.update(data=[bad, good], total=2)
    batch = TwseInstitutionalAdapter().parse(replace(raw, body=json.dumps(payload).encode()))
    assert [row.symbol for row in batch.rows] == ["SYNTH1"]
    assert [issue.code for issue in batch.issues] == ["invalid_quantity"]
