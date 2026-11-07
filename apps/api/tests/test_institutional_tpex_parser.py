"""Fixed SYNTHETIC TPEx contract data, never downloaded or imported trading data."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.models import (
    Category, Completeness, FactLineage, InstitutionalDailyReport,
    InstitutionalValidationError, QuantityUnit, SourceTiming, TradeBasis,
    TradeScope,
)
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure.tpex_parser import (
    TPEX_CONTRACT_VERSION, TPEX_ENDPOINT, TPEX_FLOW_COLUMNS, TPEX_OPENAPI_FIELDS,
    parse_tpex,
)

FIXTURE = Path(__file__).parent / "fixtures/institutional/tpex_openapi.json"
SEEN = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
SOURCE, RUN, MARKET, INSTRUMENT, REVISION = (
    UUID(f"00000000-0000-4000-8000-{value:012d}") for value in range(901, 906)
)


def synthetic_data():
    return json.loads(FIXTURE.read_bytes())


def synthetic_raw(data=None, **changes):
    values = dict(source_id=SOURCE, run_id=RUN, market_id=MARKET,
        endpoint=TPEX_ENDPOINT, content_type="application/json; charset=utf-8",
        body=json.dumps(data if data is not None else synthetic_data()).encode(),
        status_code=200, fetched_at=SEEN)
    return RawPayload(**(values | changes))


def synthetic_report(batch, *, verified_test_scope=False):
    """Use explicit synthetic identity; this is not production identity evidence."""
    scope = batch.scope
    if verified_test_scope:
        scope = TradeScope(code="synthetic_verified_scope", includes_block_trades=True,
            includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL)
    return InstitutionalDailyReport(instrument_id=INSTRUMENT, market_id=MARKET,
        trade_date=batch.trade_date, components=batch.rows[0].components,
        quantity_unit=batch.quantity_unit, scope=scope,
        lineage=FactLineage(source_id=SOURCE, source_contract_version=batch.contract_version,
            parser_version=batch.parser_version, revision_id=REVISION, run_id=RUN,
            raw_id=UUID("00000000-0000-4000-8000-000000000906"),
            source_row_locator=batch.rows[0].locator, payload_hash="0" * 64),
        timing=SourceTiming(first_seen_at=SEEN, last_seen_at=SEEN, ingested_at=SEEN))


def test_fixture_exact_twenty_key_schema_and_known_financial_values():
    fixture = synthetic_data()
    assert len(TPEX_OPENAPI_FIELDS) == len(fixture[0]) == 20
    assert set(fixture[0]) == set(TPEX_OPENAPI_FIELDS)
    batch = parse_tpex(synthetic_raw())
    assert batch.trade_date == date(2026, 11, 6)
    assert batch.contract_version == TPEX_CONTRACT_VERSION
    assert not batch.issues and len(batch.rows) == 1
    row = batch.rows[0]
    assert row.symbol == "SYNTH1" and "SYNTHETIC" in row.name
    flows = {flow.category: flow for flow in row.components}
    assert batch.quantity_unit is QuantityUnit.SHARES
    # Already shares: neither commas nor the UI's lot convention add a factor.
    assert flows[Category.FOREIGN_EX_DEALERS].buy_quantity == 1200
    assert flows[Category.FOREIGN_EX_DEALERS].net_quantity == 1000
    assert flows[Category.FOREIGN_DEALERS].net_quantity == -10
    assert flows[Category.FOREIGN_INCLUSIVE].net_quantity == 990
    assert flows[Category.INVESTMENT_TRUST].net_quantity == 200
    assert flows[Category.DEALER_TOTAL].net_quantity == 100
    assert flows[Category.THREE_MAJOR_TOTAL].net_quantity == 1300
    assert flows[Category.THREE_MAJOR_TOTAL].buy_quantity is None
    assert row.row_index == 0 and row.locator == "array/0"


def test_oddly_spaced_dealer_column_is_only_hedging_sell_without_invented_leaves():
    batch = parse_tpex(synthetic_raw())
    report = synthetic_report(batch)
    hedge = report.component(Category.DEALER_HEDGING)
    assert hedge.sell_quantity == 150
    assert hedge.buy_quantity is hedge.net_quantity is None
    assert report.component(Category.DEALER_PROPRIETARY) is None
    assert report.component(Category.DEALER_TOTAL).sell_quantity == 400
    assert report.completeness is Completeness.NOT_VERIFIED
    assert not batch.scope.is_verified
    assert batch.scope.includes_block_trades is None
    # Even separate evidence about scope could not fill the missing dealer data.
    assert synthetic_report(batch, verified_test_scope=True).completeness is Completeness.PARTIAL


def test_reordered_object_keys_have_same_components_instead_of_positional_mapping():
    data = synthetic_data()
    shuffled = [dict(reversed(list(data[0].items())))]
    expected = parse_tpex(synthetic_raw())
    actual = parse_tpex(synthetic_raw(shuffled))
    assert actual.rows[0].components == expected.rows[0].components


def test_documented_leading_space_alias_is_accepted_and_raw_spelling_preserved():
    data = synthetic_data()
    official = TPEX_OPENAPI_FIELDS[4]
    assert official.startswith(" ")
    data[0][official.strip()] = data[0].pop(official)
    batch = parse_tpex(synthetic_raw(data))
    assert not batch.issues
    assert batch.rows[0].components[0].sell_quantity == 200
    assert official.strip() in batch.rows[0].raw_row


def test_alias_collision_is_rejected_before_dictionary_overwrite():
    data = synthetic_data()
    official = TPEX_OPENAPI_FIELDS[4]
    data[0][official.strip()] = data[0][official]
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(data))


def test_literal_duplicate_json_keys_are_rejected_before_json_dict_loss():
    body = FIXTURE.read_text().replace('"Date": "1151106"', '"Date": "1151106", "Date": "1151105"')
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(body=body.encode()))


@pytest.mark.parametrize("field", TPEX_OPENAPI_FIELDS)
def test_each_missing_column_is_a_schema_error_not_zero_or_partial_schema(field):
    data = synthetic_data()
    del data[0][field]
    with pytest.raises(InstitutionalSourceError) as error:
        parse_tpex(synthetic_raw(data))
    assert error.value.code == "missing_columns"


def test_unknown_schema_is_rejected_even_if_original_twenty_columns_exist():
    data = synthetic_data()
    data[0]["UnexpectedDealerProprietaryBuy"] = "100"
    with pytest.raises(InstitutionalSourceError) as error:
        parse_tpex(synthetic_raw(data))
    assert error.value.code == "unknown_schema"


def test_a_prettified_but_undocumented_spelling_is_not_assumed_equivalent():
    data = synthetic_data()
    data[0]["Foreign Dealers-Total Sell"] = data[0].pop("Foreign Dealers-TotalSell")
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(data))


@pytest.mark.parametrize("value", ["1151301", "1150230", "20261106", "", None, True, 1151106])
def test_invalid_or_ambiguous_source_date_rejects_batch(value):
    data = synthetic_data()
    data[0]["Date"] = value
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(data))


def test_mixed_dates_reject_batch_instead_of_choosing_download_date():
    data = synthetic_data()
    data.append(data[0] | {"SecuritiesCompanyCode": "SYNTH2", "Date": "1151105"})
    with pytest.raises(InstitutionalSourceError) as error:
        parse_tpex(synthetic_raw(data))
    assert error.value.code == "mixed_trade_dates"


def test_future_source_date_rejected_in_market_local_date_semantics():
    data = synthetic_data()
    data[0]["Date"] = "1151108"
    with pytest.raises(InstitutionalSourceError) as error:
        parse_tpex(synthetic_raw(data))
    assert error.value.code == "future_trade_date"


def test_requested_historical_date_is_not_ignored_by_latest_only_contract():
    with pytest.raises(InstitutionalSourceError) as error:
        parse_tpex(synthetic_raw(), requested_date=date(2016, 11, 7))
    assert error.value.code == "historical_query_unsupported"
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(request_parameters={"date": "1151106"}))


@pytest.mark.parametrize("invalid", [-1, "-1", "1,20", 1.2, True, "NaN", "Infinity", "1e3"])
def test_bad_quantity_does_not_silently_drop_other_valid_rows(invalid):
    data = synthetic_data()
    data.append(data[0] | {"SecuritiesCompanyCode": "SYNTH2"})
    data[0][TPEX_FLOW_COLUMNS[Category.FOREIGN_EX_DEALERS][0]] = invalid
    batch = parse_tpex(synthetic_raw(data))
    assert len(batch.issues) == 1 and len(batch.rows) == 1
    assert batch.issues[0].row_index == 0 and batch.issues[0].symbol == "SYNTH1"
    assert batch.issues[0].raw_row[TPEX_FLOW_COLUMNS[Category.FOREIGN_EX_DEALERS][0]] == invalid
    assert batch.rows[0].symbol == "SYNTH2" and batch.rows[0].row_index == 1


def test_buy_sell_net_contradiction_becomes_explicit_row_issue():
    data = synthetic_data()
    data[0][TPEX_FLOW_COLUMNS[Category.FOREIGN_EX_DEALERS][2]] = "999"
    batch = parse_tpex(synthetic_raw(data))
    assert not batch.rows and len(batch.issues) == 1
    assert batch.issues[0].code == "net_mismatch"


@pytest.mark.parametrize("missing", [None, "", " ", "--"])
def test_unknown_quantity_stays_null_and_is_distinct_from_real_zero(missing):
    data = synthetic_data()
    for field in TPEX_FLOW_COLUMNS[Category.FOREIGN_EX_DEALERS]:
        original = next(key for key in data[0] if key.strip() == field)
        data[0][original] = missing
    missing_batch = parse_tpex(synthetic_raw(data))
    missing_flow = missing_batch.rows[0].components[0]
    assert missing_flow.buy_quantity is missing_flow.sell_quantity is missing_flow.net_quantity is None
    for field in TPEX_FLOW_COLUMNS[Category.FOREIGN_EX_DEALERS]:
        original = next(key for key in data[0] if key.strip() == field)
        data[0][original] = "0"
    zero_batch = parse_tpex(synthetic_raw(data))
    zero_flow = zero_batch.rows[0].components[0]
    assert (zero_flow.buy_quantity, zero_flow.sell_quantity, zero_flow.net_quantity) == (0, 0, 0)


@pytest.mark.parametrize("category,field", [
    (Category.FOREIGN_INCLUSIVE, "net_quantity"),
    (Category.THREE_MAJOR_TOTAL, "net_quantity"),
])
def test_cross_category_reconciliation_uses_existing_report_domain(category, field):
    data = synthetic_data()
    fields = TPEX_FLOW_COLUMNS[category]
    # Keep each component's own equation valid; only its subtotal relationship
    # is wrong. The shared Domain report, not a second calculator, rejects it.
    if category is Category.FOREIGN_INCLUSIVE:
        data[0][fields[0]] = "1,221"
        data[0][fields[2]] = "991"
    else:
        data[0][fields[2]] = "1,301"
    batch = parse_tpex(synthetic_raw(data))
    assert batch.rows and not batch.issues
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_report(batch)
    assert error.value.code == "aggregate_mismatch"


@pytest.mark.parametrize("data", [{}, [None], ["HTML"], [], {"stat": "error"}])
def test_unexpected_or_undated_empty_responses_are_not_fake_zero_reports(data):
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(data))


def test_html_and_wrong_endpoint_are_rejected_without_extracting_numeric_text():
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(body=b"<html>maintenance</html>", content_type="text/html"))
    with pytest.raises(InstitutionalSourceError):
        parse_tpex(synthetic_raw(endpoint="https://example.invalid/not-the-audited-source"))


def test_identity_text_is_trimmed_without_inventing_security_type_or_uuid():
    data = synthetic_data()
    data[0]["SecuritiesCompanyCode"] = "  SYNTH1  "
    data[0]["CompanyName"] = "  SYNTHETIC test only  "
    batch = parse_tpex(synthetic_raw(data))
    assert batch.rows[0].symbol == "SYNTH1"
    assert batch.rows[0].name == "SYNTHETIC test only"
    data[0]["SecuritiesCompanyCode"] = 2330
    invalid = parse_tpex(synthetic_raw(data))
    assert not invalid.rows and invalid.issues[0].code == "invalid_source_identity"


def test_source_trade_date_uses_taipei_date_even_when_utc_calendar_date_is_earlier():
    seen = datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc)
    batch = parse_tpex(synthetic_raw(fetched_at=seen))
    assert batch.trade_date == date(2026, 11, 6)
