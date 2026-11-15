"""SYNTHETIC offline contracts and exact source normalization, never live data."""
from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.application.normalization import BrokerNormalizer
from stockapp.modules.broker_flow.domain.identities import BrokerIdentityBook
from stockapp.modules.broker_flow.domain.ingestion import (
    CSV_COLUMNS, OFFLINE_CONTRACT_VERSION, OFFLINE_PARSER_VERSION, OFFLINE_SCHEMA_VERSION,
    OfflineContract, ParsedSourceBatch, SourceRow, contract_to_dict, parse_import_contract,
)
from stockapp.modules.broker_flow.domain.models import BrokerFlowError, QuantityUnit, ReportCompleteness
from stockapp.modules.broker_flow.domain.persistence import RawPayload
from test_broker_flow_domain import (
    BRANCH, DAY, INSTRUMENT, MARKET, RAW, RUN, SEEN, SOURCE,
    synthetic_identity, synthetic_scope,
)


def synthetic_contract(**changes):
    scope = synthetic_scope()
    values = dict(contract_version=OFFLINE_CONTRACT_VERSION, source_id=str(SOURCE),
        source_namespace="SYNTHETIC-namespace", source_contract_version="SYNTHETIC-contract-v1",
        schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION,
        market_id=str(MARKET), market_code="SYNTHETIC_TW", market_timezone="Asia/Taipei",
        identity_version="SYNTHETIC-identity-v1", identity_policy_version="as_reported_branch_v1",
        record_kind="branch_totals", source_quantity_unit="shares", price_basis="raw",
        price_quantity_basis="per_canonical_unit", currency="TWD",
        usage=dict(offline_import=True, raw_retention=True, revision_retention=True,
            internal_processing=True, evidence_ref="SYNTHETIC rights evidence, not a license"),
        scopes=[{key: str(value) if isinstance(value, UUID) else value for key, value in asdict(scope).items()}],
        reports=[dict(report_id="SYNTHETIC-report", instrument_symbol="SYNTHETIC", trade_date=DAY.isoformat(),
            scope_code=scope.code, scope_version=scope.version, expected_rows=1,
            branch_universe_complete=True, evidence_ref="SYNTHETIC complete-delivery evidence")],
        instruments=[dict(instrument_id=str(INSTRUMENT), market_id=str(MARKET), market_code="SYNTHETIC_TW",
            symbol="SYNTHETIC", quantity_unit="shares", currency="TWD", valid_from="2020-01-01",
            valid_through="2030-12-31", evidence_ref="SYNTHETIC dated UUID evidence")], unit_conversions=[])
    return parse_import_contract(values | changes)


def synthetic_source_row(index=0, **changes):
    scope = synthetic_scope()
    values = dict.fromkeys(CSV_COLUMNS, "") | dict(schema_version=OFFLINE_SCHEMA_VERSION,
        report_id="SYNTHETIC-report", market_code="SYNTHETIC_TW", instrument_symbol="SYNTHETIC",
        trade_date=DAY.isoformat(), scope_code=scope.code, scope_version=scope.version,
        branch_code="000aB", quantity_unit="shares", buy_quantity="1000", sell_quantity="100",
        reported_net_quantity="900", currency="TWD", row_id="SYNTHETIC-row-" + str(index)) | changes
    return SourceRow(row_index=index, row_locator="SYNTHETIC.csv/line=" + str(index+2), fields=tuple(values.items()))


def synthetic_raw():
    return RawPayload(id=RAW, run_id=RUN, source_id=SOURCE,
        source_contract_version="SYNTHETIC-contract-v1", endpoint="offline://synthetic/report.csv",
        body=b"SYNTHETIC body, not an official report", content_type="text/csv", status_code=200,
        fetched_at=SEEN)


class SyntheticInstrumentReader:
    def get_by_id(self, identifier):
        return SimpleNamespace(instrument_id=INSTRUMENT, market_id=MARKET, market_code="SYNTHETIC_TW",
            symbol="SYNTHETIC-CURRENT-NAME", quantity_unit="shares", quote_currency="TWD") if identifier == INSTRUMENT else None


def normalize(rows, contract=None, identities=None, reader=None):
    raw = synthetic_raw()
    batch = ParsedSourceBatch(raw_id=raw.id, raw_sha256=raw.sha256, schema_version=OFFLINE_SCHEMA_VERSION,
                              parser_version=OFFLINE_PARSER_VERSION, rows=tuple(rows))
    return BrokerNormalizer(contract or synthetic_contract(),
        BrokerIdentityBook(identities=tuple(identities or (synthetic_identity(),))),
        reader or SyntheticInstrumentReader()).normalize(raw, batch, ingested_at=SEEN+timedelta(seconds=1))


def with_rows(contract, count):
    return replace(contract, reports=(replace(contract.reports[0], expected_rows=count),))


def test_contract_roundtrip_is_exact_and_opaque_codes_remain_strings():
    contract = synthetic_contract()
    assert parse_import_contract(contract.to_dict()) == contract
    assert contract_to_dict(contract)["reports"][0]["instrument_symbol"] == "SYNTHETIC"
    assert contract.scopes[0].verified is True


@pytest.mark.parametrize("key", ["offline_import", "raw_retention", "revision_retention", "internal_processing"])
@pytest.mark.parametrize("invalid", [False, 1, "true", None])
def test_usage_requires_explicit_booleans_and_evidence(key, invalid):
    data = synthetic_contract().to_dict()
    data["usage"][key] = invalid
    with pytest.raises(BrokerFlowError):
        parse_import_contract(data)


@pytest.mark.parametrize("change", [
    {"contract_version": "unknown"}, {"schema_version": "twse-unverified"},
    {"parser_version": "unknown"}, {"source_quantity_unit": "thousand_shares"},
    {"record_kind": "unknown"}, {"automatic_download": True},
])
def test_contract_rejects_unknown_versions_units_and_extra_rights(change):
    with pytest.raises(BrokerFlowError):
        synthetic_contract(**change)


def test_complete_quantity_report_uses_dated_uuid_evidence_not_current_symbol():
    group = normalize((synthetic_source_row(),))[0]
    assert group.report.completeness is ReportCompleteness.COMPLETE
    assert group.report.trades[0].net_quantity == 900
    assert group.report.trades[0].buy_amount is None
    assert group.issues[0].disposition == "accepted"


@pytest.mark.parametrize("price", ["12", "0", ""])
def test_unknown_price_currency_keeps_valid_quantities_without_inventing_money(price):
    contract = synthetic_contract(record_kind="price_buckets", currency=None)
    group = normalize((synthetic_source_row(price=price, currency=""),), contract)[0]
    assert group.report.completeness is ReportCompleteness.COMPLETE
    assert group.report.currency is None and group.report.trades[0].net_quantity == 900
    assert group.report.trades[0].buy_amount is group.report.trades[0].sell_amount is None
    assert group.issues[0].disposition == "accepted"
    assert group.report.lineage.source_published_at is None
    assert group.report.lineage.first_seen_at == SEEN


def test_price_buckets_are_not_duplicate_branches_and_exact_amount_differs_from_mean_times_net():
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 2)
    rows = (synthetic_source_row(0, price="10", buy_quantity="50", sell_quantity="20", reported_net_quantity="30"),
            synthetic_source_row(1, price="12", buy_quantity="50", sell_quantity="30", reported_net_quantity="20"))
    trade = normalize(rows, contract)[0].report.trades[0]
    assert (trade.buy_quantity, trade.sell_quantity, trade.net_quantity) == (100, 50, 50)
    assert (trade.buy_amount, trade.sell_amount) == (Decimal(1100), Decimal(560))
    assert trade.buy_amount-trade.sell_amount == Decimal(540)


def test_branch_displayed_average_never_becomes_exact_money():
    trade = normalize((synthetic_source_row(price="11"),))[0].report.trades[0]
    assert trade.net_quantity == 900 and trade.buy_amount is None and trade.sell_amount is None


@pytest.mark.parametrize("unknown_price", ["", "0"])
def test_unknown_bucket_price_preserves_quantities_and_unknown_amounts(unknown_price):
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 2)
    rows = (synthetic_source_row(0, price="10"), synthetic_source_row(1, price=unknown_price))
    trade = normalize(rows, contract)[0].report.trades[0]
    assert trade.buy_quantity == 2000
    assert trade.buy_amount is None and trade.sell_amount is None


def test_reported_money_is_verified_against_exact_price_when_available():
    row = synthetic_source_row(price="10", buy_amount="9999")
    group = normalize((row,), synthetic_contract(record_kind="price_buckets"))[0]
    assert group.report is None and group.issues[0].reason == "source_amount_mismatch"


def test_missing_quantity_is_not_zero_and_missing_amount_does_not_poison_known_side():
    row = synthetic_source_row(buy_quantity="", sell_quantity="0", reported_net_quantity="")
    group = normalize((row,))[0]
    assert group.report.completeness is ReportCompleteness.PARTIAL
    assert group.report.trades[0].buy_quantity is None
    assert group.report.trades[0].sell_quantity == 0
    assert group.report.trades[0].net_quantity is None


@pytest.mark.parametrize("changes,reason", [
    ({"buy_quantity": "-1"}, "invalid_numeric_value"),
    ({"buy_quantity": "1,00"}, "invalid_numeric_value"),
    ({"buy_quantity": "1e3"}, "invalid_numeric_value"),
    ({"price": "NaN"}, "invalid_numeric_value"),
    ({"reported_net_quantity": "901"}, "net_quantity_mismatch"),
    ({"trade_date": "115/01/09"}, "invalid_source_date"),
    ({"trade_date": "2026-01-08"}, "source_date_mismatch"),
    ({"quantity_unit": "lots"}, "source_unit_mismatch"),
    ({"market_code": "OTHER"}, "source_market_mismatch"),
    ({"branch_code": "000ab"}, "unknown_branch"),
    ({"source_published_at": "2026-01-09T18:00:00"}, "invalid_source_timestamp"),
])
def test_invalid_rows_have_auditable_reasons(changes, reason):
    group = normalize((synthetic_source_row(**changes),))[0]
    assert group.report is None
    assert group.issues[0].reason == reason


def test_comma_numbers_are_exact():
    trade = normalize((synthetic_source_row(buy_quantity="1,000", sell_quantity="100"),))[0].report.trades[0]
    assert trade.buy_quantity == 1000 and trade.net_quantity == 900


def test_duplicate_source_ids_are_not_silently_added():
    row = synthetic_source_row()
    rows = (row, synthetic_source_row(1, row_id=row.values["row_id"]))
    group = normalize(rows, with_rows(synthetic_contract(record_kind="price_buckets"), 2))[0]
    assert group.report is None
    assert [issue.reason for issue in group.issues] == ["duplicate_source_row_id"]*2


def test_duplicate_daily_branch_rows_are_not_confused_with_price_buckets():
    group = normalize((synthetic_source_row(), synthetic_source_row(1)), with_rows(synthetic_contract(), 2))[0]
    assert group.report is None
    assert {issue.reason for issue in group.issues} == {"duplicate_branch_total"}


def test_unknown_branch_only_blocks_its_report_other_reports_continue():
    contract = synthetic_contract()
    second = replace(contract.reports[0], report_id="SYNTHETIC-second", trade_date=DAY-timedelta(days=1))
    contract = replace(contract, reports=(*contract.reports, second))
    groups = normalize((synthetic_source_row(branch_code="UNKNOWN"), synthetic_source_row(1,
        report_id=second.report_id, trade_date=second.trade_date.isoformat())), contract)
    assert groups[0].report is None and groups[0].issues[0].disposition == "unresolved"
    assert groups[1].report.completeness is ReportCompleteness.COMPLETE


def test_unassigned_rows_keep_audit_and_block_false_completeness():
    groups = normalize((synthetic_source_row(), synthetic_source_row(1, report_id="UNKNOWN")))
    assert groups[0].report.completeness is ReportCompleteness.PARTIAL
    assert groups[1].report_id == "__unassigned__" and groups[1].report is None
    assert sum(len(group.issues) for group in groups) == 2


def test_missing_report_and_incomplete_delivery_are_explicit():
    group = normalize((), synthetic_contract())[0]
    assert group.report is None and group.reason == "declared_report_missing"
    partial = normalize((synthetic_source_row(),), with_rows(synthetic_contract(), 2))[0]
    assert partial.report.completeness is ReportCompleteness.PARTIAL
    assert partial.reason == "report_row_count_mismatch"


def test_lot_multiplier_is_evidenced_per_instrument_date_and_not_assumed_1000():
    data = synthetic_contract(source_quantity_unit="lots", record_kind="price_buckets").to_dict()
    data["unit_conversions"] = [dict(instrument_id=str(INSTRUMENT), source_unit="lots", target_unit="shares",
        multiplier="200", valid_from="2020-01-01", valid_through="2030-12-31", evidence_ref="SYNTHETIC 200/share-lot")]
    row = synthetic_source_row(quantity_unit="lots", buy_quantity="0.5", sell_quantity="0.25",
                               reported_net_quantity="0.25", price="0.10000001")
    canonical = normalize((row,), parse_import_contract(data))[0].report.trades[0]
    assert (canonical.buy_quantity, canonical.sell_quantity, canonical.net_quantity) == (100, 50, 50)
    assert canonical.buy_amount == Decimal("10.00000100")
    # The same raw price expressed per source lot yields a different exact amount.
    data["price_quantity_basis"] = "per_source_unit"
    canonical = normalize((row,), parse_import_contract(data))[0].report.trades[0]
    assert canonical.buy_amount == Decimal("0.050000005")


def test_unknown_lot_multiplier_and_fractional_base_units_are_rejected():
    row = synthetic_source_row(quantity_unit="lots")
    group = normalize((row,), synthetic_contract(source_quantity_unit="lots"))[0]
    assert group.report is None and group.issues[0].reason == "unverified_unit_conversion"
    group = normalize((synthetic_source_row(buy_quantity="0.5"),))[0]
    assert group.report is None and group.issues[0].reason == "invalid_numeric_value"


def test_decimal_calculation_is_independent_of_ambient_precision():
    row = synthetic_source_row(price="0.10000001", buy_quantity="3", sell_quantity="1", reported_net_quantity="2")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        trade = normalize((row,), synthetic_contract(record_kind="price_buckets"))[0].report.trades[0]
    assert trade.buy_amount-trade.sell_amount == Decimal("0.20000002")


def test_all_rows_keep_original_locators_but_report_uses_stable_source_ids():
    row = synthetic_source_row()
    group = normalize((row,))[0]
    assert group.issues[0].row_locator == row.row_locator
    assert group.report.trades[0].row_locators == ("offline-row:" + row.values["row_id"],)


def test_member_line_and_row_order_changes_do_not_change_normalized_financial_snapshot():
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 2)
    first_row = synthetic_source_row(0, price="10")
    second_row = synthetic_source_row(1, price="12")
    first = normalize((first_row, second_row), contract)[0].report
    reordered = (replace(second_row, row_index=0, row_locator="OTHER/archive/member2.csv/line=99"),
                 replace(first_row, row_index=1, row_locator="OTHER/archive/member1.csv/line=123"))
    second_group = normalize(reordered, contract)[0]
    assert second_group.report.trades == first.trades
    assert replace(second_group.report, revision_id=first.revision_id) == first
    assert second_group.issues[0].row_locator != second_row.row_locator


def test_same_price_with_different_verified_original_row_ids_is_not_guessed_duplicate():
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 2)
    trade = normalize((synthetic_source_row(0, price="10"), synthetic_source_row(1, price="10")), contract)[0].report.trades[0]
    assert trade.buy_quantity == 2000 and trade.buy_amount == Decimal(20000)
    assert len(trade.row_locators) == 2


def test_zero_price_sentinel_does_not_discard_explicit_verified_side_amounts():
    row = synthetic_source_row(price="0", buy_amount="100", sell_amount="10")
    trade = normalize((row,), synthetic_contract(record_kind="price_buckets"))[0].report.trades[0]
    assert (trade.buy_amount, trade.sell_amount) == (Decimal(100), Decimal(10))


def test_source_statistical_date_uses_market_date_across_utc_midnight():
    row = synthetic_source_row(source_generated_at="2026-01-08T16:01:00Z",
                               source_published_at="2026-01-08T16:02:00Z")
    group = normalize((row,))[0]
    assert group.report.trade_date == DAY
    assert group.report.lineage.source_published_at.isoformat() == "2026-01-08T16:02:00+00:00"
    rejected = normalize((synthetic_source_row(source_generated_at="2026-01-08T15:59:00Z"),))[0]
    assert rejected.report is None and rejected.issues[0].reason == "source_timestamp_before_trade_date"


def test_equivalent_timezone_offsets_are_one_source_observation():
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 2)
    first = synthetic_source_row(0, source_published_at="2026-01-09T17:00:00+08:00")
    second = synthetic_source_row(1, source_published_at="2026-01-09T09:00:00Z")
    group = normalize((first, second), contract)[0]
    assert group.report.completeness is ReportCompleteness.COMPLETE
    inconsistent = replace(second, fields=tuple((key, "2026-01-09T09:01:00Z" if key == "source_published_at" else value)
                                                for key, value in second.fields))
    group = normalize((first, inconsistent), contract)[0]
    assert group.report is None and group.reason == "inconsistent_source_timing"


def test_invalid_timezone_and_reserved_internal_report_id_are_rejected():
    with pytest.raises(BrokerFlowError, match="invalid_market_timezone"):
        synthetic_contract(market_timezone="SYNTHETIC/not-a-zone")
    contract = synthetic_contract()
    with pytest.raises(BrokerFlowError, match="reserved_report_id"):
        replace(contract.reports[0], report_id="__unassigned__")


def test_structural_bad_row_never_becomes_a_sum_of_only_valid_branch_buckets():
    contract = with_rows(synthetic_contract(record_kind="price_buckets"), 3)
    identity = synthetic_identity(id=UUID(int=401), branch_id=UUID(int=402), branch_code="SECOND")
    rows = (synthetic_source_row(0, price="10"), synthetic_source_row(1, buy_quantity="broken"),
            synthetic_source_row(2, branch_code="SECOND", price="20"))
    group = normalize(rows, contract, identities=(synthetic_identity(), identity))[0]
    assert group.report.completeness is ReportCompleteness.PARTIAL
    assert len(group.report.trades) == 1
    assert group.report.trades[0].branch_id == identity.branch_id
    assert group.report.trades[0].buy_quantity == 1000
    assert len(group.issues) == 3 and group.issues[1].disposition == "rejected"


def test_multiplier_evidence_changes_only_for_its_verified_instrument_date():
    contract = synthetic_contract(source_quantity_unit="lots")
    prior = replace(contract.reports[0], report_id="SYNTHETIC-prior", trade_date=DAY-timedelta(days=1))
    data = replace(contract, reports=(*contract.reports, prior)).to_dict()
    data["unit_conversions"] = [dict(instrument_id=str(INSTRUMENT), source_unit="lots", target_unit="shares",
        multiplier=str(multiplier), valid_from=day.isoformat(), valid_through=day.isoformat(), evidence_ref="SYNTHETIC dated conversion")
        for day, multiplier in ((DAY, 200), (prior.trade_date, 100))]
    rows = (synthetic_source_row(0, quantity_unit="lots", buy_quantity="1", sell_quantity="0", reported_net_quantity="1"),
            synthetic_source_row(1, report_id=prior.report_id, trade_date=prior.trade_date.isoformat(),
                quantity_unit="lots", buy_quantity="1", sell_quantity="0", reported_net_quantity="1"))
    groups = normalize(rows, parse_import_contract(data))
    assert [group.report.trades[0].buy_quantity for group in groups] == [200, 100]
    fractional = synthetic_source_row(0, quantity_unit="lots", buy_quantity="0.001", sell_quantity="0", reported_net_quantity="")
    group = normalize((fractional,), parse_import_contract(data))[0]
    assert group.report is None and group.issues[0].reason == "invalid_canonical_quantity"


def test_raw_lineage_mismatch_is_not_normalized():
    raw = synthetic_raw()
    batch = ParsedSourceBatch(raw_id=UUID(int=999), raw_sha256=raw.sha256,
        schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION, rows=(synthetic_source_row(),))
    normalizer = BrokerNormalizer(synthetic_contract(), BrokerIdentityBook(identities=(synthetic_identity(),)), SyntheticInstrumentReader())
    with pytest.raises(BrokerFlowError, match="parsed_raw_lineage_mismatch"):
        normalizer.normalize(raw, batch, ingested_at=SEEN)


def test_source_dto_requires_named_string_fields_and_bounded_physical_locators():
    row = synthetic_source_row()
    assert replace(row, row_locator="x"*512).row_locator == "x"*512
    with pytest.raises(BrokerFlowError):
        replace(row, row_locator="x"*513)
    with pytest.raises(BrokerFlowError, match="invalid_source_row_fields"):
        replace(row, fields={"aa": "value"})


def test_different_source_aliases_cannot_choose_first_snapshot_for_same_resolved_uuid():
    contract = synthetic_contract()
    alias = replace(contract.reports[0], report_id="SYNTHETIC-alias-report", instrument_symbol="SYNTHETIC-alias")
    contract = replace(contract, reports=(*contract.reports, alias),
        instruments=(*contract.instruments, replace(contract.instruments[0], symbol=alias.instrument_symbol)))
    rows = (synthetic_source_row(), synthetic_source_row(1, report_id=alias.report_id, instrument_symbol=alias.instrument_symbol))
    groups = normalize(rows, contract)
    assert all(group.report is None and group.reason == "duplicate_resolved_report_identity" for group in groups)
    assert [group.issues[0].reason for group in groups] == ["duplicate_resolved_report_identity"]*2
    # A missing alternate snapshot is not permission to silently choose the first.
    assert normalize(rows[:1], contract)[0].report is None


def test_same_instrument_and_date_with_distinct_scope_ids_remains_separate():
    contract = synthetic_contract()
    other_scope = replace(contract.scopes[0], id=UUID(int=991), code="SYNTHETIC-block",
                          block_trade_included=True, odd_lot_included=False)
    second = replace(contract.reports[0], report_id="SYNTHETIC-block-report", scope_code=other_scope.code)
    contract = replace(contract, scopes=(*contract.scopes, other_scope), reports=(*contract.reports, second))
    groups = normalize((synthetic_source_row(), synthetic_source_row(1, report_id=second.report_id,
        scope_code=second.scope_code)), contract)
    assert all(group.report.completeness is ReportCompleteness.COMPLETE for group in groups)
    assert groups[0].report.natural_key != groups[1].report.natural_key


def test_archive_bytes_are_hashed_once_for_many_reports():
    original = synthetic_raw()

    class CountingRaw:
        calls = 0

        def __getattr__(self, name):
            return getattr(original, name)

        @property
        def sha256(self):
            self.calls += 1
            return original.sha256

    raw = CountingRaw()
    contract = synthetic_contract()
    reports = tuple(replace(contract.reports[0], report_id="SYNTHETIC-"+str(index),
                            trade_date=DAY-timedelta(days=index)) for index in range(25))
    contract = replace(contract, reports=reports)
    rows = tuple(synthetic_source_row(index, report_id=declaration.report_id,
        trade_date=declaration.trade_date.isoformat()) for index, declaration in enumerate(reports))
    batch = ParsedSourceBatch(raw_id=original.id, raw_sha256=original.sha256,
        schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION, rows=rows)
    groups = BrokerNormalizer(contract, BrokerIdentityBook(identities=(synthetic_identity(),)),
        SyntheticInstrumentReader()).normalize(raw, batch, ingested_at=SEEN)
    assert len(groups) == 25 and all(group.report is not None for group in groups)
    assert raw.calls == 1
