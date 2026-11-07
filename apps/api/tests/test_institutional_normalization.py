"""SYNTHETIC end-to-end parser -> typed Domain tests, no storage or network."""
from dataclasses import replace
from datetime import date, timedelta
import json
from uuid import UUID, uuid4

import pytest

from test_institutional_sources import FETCHED, MARKET, SOURCE, RUN, synthetic_raw
from stockapp.modules.institutional.application.normalization import InstitutionalNormalizer
from stockapp.modules.institutional.domain.calculations import InstitutionalCalculator
from stockapp.modules.institutional.domain.models import Category, Completeness, QuantityUnit
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError, InstrumentBinding
from stockapp.modules.institutional.infrastructure.providers import TwseInstitutionalAdapter, TpexInstitutionalAdapter


class SyntheticResolver:
    """Explicit synthetic dated identity evidence, not a production registry."""
    def __init__(self, binding=None, *, missing=False):
        self.binding, self.missing = binding, missing

    def resolve(self, *, market_id, symbol, trade_date):
        if self.missing:
            return None
        return self.binding or InstrumentBinding(instrument_id=UUID(int=304), market_id=market_id,
            symbol=symbol, quantity_unit=QuantityUnit.SHARES, valid_from=trade_date,
            valid_through=trade_date, evidence_version="synthetic-identity-v1")


def normalized(name="twse_modern", *, resolver=None, payload=None):
    raw = synthetic_raw(name)
    if payload is not None:
        raw = replace(raw, body=json.dumps(payload, ensure_ascii=False).encode())
    adapter = TpexInstitutionalAdapter() if name == "tpex_openapi" else TwseInstitutionalAdapter()
    batch = adapter.parse(raw)
    return InstitutionalNormalizer(resolver or SyntheticResolver()).normalize(raw, batch, ingested_at=FETCHED)


def test_modern_twse_fixed_values_lineage_scope_and_no_double_counting():
    result = normalized()
    assert not result.issues and len(result.items) == 1
    item, report = result.items[0], result.reports[0]
    assert report.completeness is Completeness.COMPLETE
    assert report.component(Category.FOREIGN_EX_DEALERS).buy_quantity == 1000
    assert report.component(Category.FOREIGN_DEALERS).net_quantity == 150
    assert report.component(Category.DEALER_PROPRIETARY).net_quantity == 50
    assert report.component(Category.DEALER_HEDGING).net_quantity == -100
    assert InstitutionalCalculator().dealer_total(report).value == -50
    assert InstitutionalCalculator().three_major_total(report).value == 750
    assert report.scope.includes_block_trades is True and report.scope.includes_odd_lots is True
    assert report.scope.includes_after_hours is True and report.scope.basis.value == "original"
    assert report.quantity_unit is QuantityUnit.SHARES
    assert (report.lineage.run_id, report.lineage.source_id) == (RUN, SOURCE)
    assert report.lineage.source_contract_version == "twse-t86-modern-19-v1"
    assert report.lineage.parser_version == "1.0.0" and report.lineage.promotion_policy_version == "not_applied"
    assert report.timing.source_published_on is report.timing.source_published_at is None
    assert report.timing.first_seen_at == report.timing.last_seen_at == FETCHED
    assert item.identity_evidence_version == "synthetic-identity-v1"
    assert item.locator == "data/0" and item.row_index == 0


def test_tpex_never_invents_missing_self_or_hedging_components():
    result = normalized("tpex_openapi")
    assert not result.issues
    report = result.reports[0]
    assert report.completeness is not Completeness.COMPLETE and not report.scope.is_verified
    assert report.component(Category.DEALER_PROPRIETARY) is None
    hedge = report.component(Category.DEALER_HEDGING)
    assert hedge.sell_quantity == 150 and hedge.buy_quantity is hedge.net_quantity is None
    assert report.component(Category.DEALER_TOTAL).net_quantity == 100
    assert report.component(Category.THREE_MAJOR_TOTAL).net_quantity == 1300
    assert report.component(Category.FOREIGN_INCLUSIVE).net_quantity == 990
    assert InstitutionalCalculator().three_major_total(report).value is None


def test_legacy_foreign_is_not_assumed_to_be_modern_classification():
    report = normalized("twse_legacy").reports[0]
    assert report.component(Category.FOREIGN_EX_DEALERS) is None
    assert report.component(Category.FOREIGN_UNSPECIFIED).mapping_status.value == "unverified"
    assert report.completeness is Completeness.NOT_VERIFIED
    assert InstitutionalCalculator().three_major_total(report).value is None


@pytest.mark.parametrize("column", ["自營商買賣超股數", "三大法人買賣超股數"])
def test_inconsistent_twse_subtotal_rejected_with_other_rows_retained(column):
    payload = json.loads(synthetic_raw().body)
    original = payload["data"][0]
    bad = list(original)
    bad[0] = "SYNTH_BAD"
    bad[payload["fields"].index(column)] = "999"
    payload.update(data=[bad, original], total=2)
    result = normalized(payload=payload)
    assert len(result.reports) == 1
    assert [(issue.symbol, issue.code) for issue in result.issues] == [("SYNTH_BAD", "aggregate_mismatch")]


@pytest.mark.parametrize("changes", [
    {"TotalDifference": "1301"},
    {"ForeignInvestorsInclude MainlandAreaInvestors-Difference": "991",
     "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy": "1,221"},
])
def test_tpex_source_totals_and_foreign_inclusive_are_reconciled_by_existing_domain(changes):
    payload = json.loads(synthetic_raw("tpex_openapi").body)
    payload[0].update(changes)
    result = normalized("tpex_openapi", payload=payload)
    assert not result.reports and result.issues[0].code == "aggregate_mismatch"


def test_missing_number_stays_none_and_zero_remains_zero():
    payload = json.loads(synthetic_raw().body)
    row, fields = payload["data"][0], payload["fields"]
    row[fields.index("外陸資買進股數(不含外資自營商)")] = "--"
    row[fields.index("外陸資賣出股數(不含外資自營商)")] = "0"
    report = normalized(payload=payload).reports[0]
    flow = report.component(Category.FOREIGN_EX_DEALERS)
    assert flow.buy_quantity is None and flow.sell_quantity == 0
    assert report.completeness is Completeness.PARTIAL


def test_unknown_instrument_is_explicit_unresolved_issue_without_generated_uuid():
    result = normalized(resolver=SyntheticResolver(missing=True))
    assert result.reports == () and result.issues[0].code == "unknown_instrument"
    assert result.issues[0].symbol == "SYNTH1" and result.issues[0].raw_row


@pytest.mark.parametrize("change,code", [
    ({"market_id": UUID(int=999)}, "instrument_identity_mismatch"),
    ({"symbol": "OTHER"}, "instrument_identity_mismatch"),
    ({"quantity_unit": QuantityUnit.UNITS}, "instrument_unit_mismatch"),
    ({"valid_from": date(2026, 11, 7)}, "historical_identity_unresolved"),
    ({"valid_through": date(2026, 11, 5)}, "historical_identity_unresolved"),
])
def test_dated_identity_market_and_unit_must_be_verified(change, code):
    binding = InstrumentBinding(instrument_id=UUID(int=304), market_id=MARKET, symbol="SYNTH1",
        quantity_unit=QuantityUnit.SHARES, valid_from=date(2026, 11, 1), valid_through=date(2026, 11, 30),
        evidence_version="synthetic-identity-v1")
    result = normalized(resolver=SyntheticResolver(replace(binding, **change)))
    assert not result.reports and result.issues[0].code == code


def test_duplicate_symbols_reject_both_rows_instead_of_last_writer_wins():
    payload = json.loads(synthetic_raw().body)
    payload["data"] *= 2
    payload["total"] = 2
    result = normalized(payload=payload)
    assert not result.reports
    assert [issue.code for issue in result.issues] == ["duplicate_symbol", "duplicate_symbol"]


@pytest.mark.parametrize("change", [{"id": uuid4()}, {"body": b"different raw bytes"}])
def test_parsed_batch_cannot_be_attached_to_another_raw_payload(change):
    raw = synthetic_raw()
    batch = TwseInstitutionalAdapter().parse(raw)
    with pytest.raises(InstitutionalSourceError, match="parsed_raw_lineage_mismatch"):
        InstitutionalNormalizer(SyntheticResolver()).normalize(replace(raw, **change), batch, ingested_at=FETCHED)


def test_duplicate_source_row_indices_are_not_accepted():
    raw = synthetic_raw()
    batch = TwseInstitutionalAdapter().parse(raw)
    batch = replace(batch, rows=(batch.rows[0], batch.rows[0]))
    with pytest.raises(InstitutionalSourceError, match="duplicate_row_index"):
        InstitutionalNormalizer(SyntheticResolver()).normalize(raw, batch, ingested_at=FETCHED)


def test_repeat_normalization_has_same_facts_hash_but_no_canonical_side_effects():
    first, second = normalized().reports[0], normalized().reports[0]
    assert first.lineage.payload_hash == second.lineage.payload_hash
    assert first.lineage.revision_id != second.lineage.revision_id
    assert first.lineage.promotion_policy_version == "not_applied"
