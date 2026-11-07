"""Pure persistence contracts with fixed SYNTHETIC observations only.

No HTTP, SQLAlchemy Session, database, container, or real institutional dataset.
"""
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.models import (
    Category, InstitutionalValidationError, MappingStatus, QuantityUnit, TradeBasis,
)
from stockapp.modules.institutional.domain.persistence import (
    ImportMode, PromotionPolicy, RawPayload, SourceAudit, report_fingerprint,
    scope_values,
)
from test_institutional_models import (
    DAY, MARKET, SEEN, SOURCE, flow, synthetic_lineage, synthetic_report,
    synthetic_scope,
)

RUN = UUID("00000000-0000-4000-8000-000000000801")
OTHER = UUID("00000000-0000-4000-8000-000000000802")


def synthetic_raw(**changes):
    values = dict(run_id=RUN, source_id=SOURCE, market_id=MARKET,
        endpoint="https://example.invalid/synthetic/institutional?date=20261105",
        body=b"synthetic raw bytes, not real institutional trades", content_type="text/plain",
        status_code=200, fetched_at=SEEN)
    return RawPayload(**(values | changes))


def test_normalized_fingerprint_is_order_independent_and_has_no_observation_clock():
    original = synthetic_report()
    changed = replace(original,
        components=tuple(reversed(original.components)),
        lineage=replace(original.lineage, revision_id=OTHER, run_id=OTHER, raw_id=OTHER,
            parser_version="synthetic-parser-v2", promotion_policy_version="synthetic-policy-v2",
            source_row_locator="synthetic/table/99/row/2", payload_hash="b" * 64),
        timing=replace(original.timing, first_seen_at=SEEN + timedelta(days=1),
            last_seen_at=SEEN + timedelta(days=1), ingested_at=SEEN + timedelta(days=2),
            source_published_on=DAY + timedelta(days=1)))
    assert report_fingerprint(original) == report_fingerprint(changed)
    assert len(report_fingerprint(original)) == 64


@pytest.mark.parametrize("kind", ["source", "contract", "taxonomy", "unit", "scope",
    "scope_flag", "basis", "instrument", "market", "date"])
def test_revision_identity_and_meaning_are_in_fingerprint(kind):
    original = synthetic_report()
    changes = {
        "source": dict(lineage=replace(original.lineage, source_id=OTHER)),
        "contract": dict(lineage=replace(original.lineage, source_contract_version="synthetic-v2")),
        "taxonomy": dict(taxonomy_version="synthetic-taxonomy-v2"),
        "unit": dict(quantity_unit=QuantityUnit.UNITS),
        "scope": dict(scope=replace(original.scope, code="synthetic_other_universe")),
        "scope_flag": dict(scope=replace(original.scope, includes_block_trades=False)),
        "basis": dict(scope=replace(original.scope, basis=TradeBasis.CORRECTED)),
        "instrument": dict(instrument_id=OTHER),
        "market": dict(market_id=OTHER),
        "date": dict(trade_date=DAY + timedelta(days=1)),
    }
    assert report_fingerprint(replace(original, **changes[kind])) != report_fingerprint(original)


def test_missing_and_zero_and_mapping_status_are_distinct_revision_contents():
    unknown = synthetic_report(components=(flow(Category.FOREIGN_EX_DEALERS),))
    zero = replace(unknown, components=(flow(Category.FOREIGN_EX_DEALERS, 0, 0, 0),))
    net_only = replace(unknown, components=(flow(Category.FOREIGN_EX_DEALERS, net=0),))
    unverified = replace(zero, components=(replace(zero.components[0], mapping_status=MappingStatus.UNVERIFIED),))
    assert len({report_fingerprint(x) for x in (unknown, zero, net_only, unverified)}) == 4


def test_full_scope_definition_preserves_unknown_flags_without_zero_fill():
    values = scope_values(synthetic_scope(includes_block_trades=None))
    assert values == dict(code="synthetic_all_original", includes_block_trades=None,
        includes_odd_lots=True, includes_after_hours=True, basis="original")


def test_raw_hash_is_exact_bytes_and_request_metadata_is_copied():
    metadata = {"date": "20261105", "filters": {"scope": "synthetic"}}
    raw = synthetic_raw(request_parameters=metadata)
    metadata["filters"]["scope"] = "changed outside snapshot"
    assert raw.request_parameters["filters"]["scope"] == "synthetic"
    assert raw.sha256 == sha256(raw.body).hexdigest()
    assert synthetic_raw(body=raw.body + b"\n").sha256 != raw.sha256


@pytest.mark.parametrize("changes,code", [
    ({"body": "not bytes"}, "invalid_raw_body"),
    ({"status_code": True}, "invalid_http_status"),
    ({"status_code": 99}, "invalid_http_status"),
    ({"status_code": 600}, "invalid_http_status"),
    ({"request_method": "DELETE"}, "invalid_request_method"),
    ({"endpoint": "https://user:password@example.invalid/synthetic"}, "unsafe_endpoint"),
    ({"endpoint": "https://example.invalid/synthetic#fragment"}, "unsafe_endpoint"),
    ({"endpoint": "https://example.invalid/synthetic?TOKEN=synthetic-secret"}, "unsafe_parameters"),
    ({"request_parameters": {"api_key": "synthetic-secret"}}, "unsafe_parameters"),
    ({"request_parameters": {"invalid": float("nan")}}, "invalid_parameters"),
    ({"request_parameters": []}, "invalid_parameters"),
])
def test_raw_contract_rejects_invalid_or_sensitive_metadata(changes, code):
    with pytest.raises(InstitutionalValidationError) as error:
        synthetic_raw(**changes)
    assert error.value.code == code


@pytest.mark.parametrize("mode,replay", [(ImportMode.SYNC, OTHER), (ImportMode.REPLAY, None),
    (ImportMode.REPLAY, RUN)])
def test_source_audit_replay_requires_another_source_run(mode, replay):
    with pytest.raises(InstitutionalValidationError, match="Replay"):
        SourceAudit(run_id=RUN, source_id=SOURCE, market_id=MARKET,
            mode=mode, replay_of_run_id=replay)


def test_explicit_replay_and_raw_reference_are_valid_and_non_self_referential():
    audit = SourceAudit(run_id=RUN, source_id=SOURCE, market_id=MARKET,
        mode=ImportMode.REPLAY, requested_trade_date=DAY, replay_of_run_id=OTHER)
    raw = synthetic_raw(replay_of_raw_id=OTHER)
    assert audit.replay_of_run_id == raw.replay_of_raw_id == OTHER
    with pytest.raises(InstitutionalValidationError, match="itself"):
        replace(raw, replay_of_raw_id=raw.id)


@pytest.mark.parametrize("kind", ["source", "contract", "taxonomy", "unit"])
def test_promotion_policy_matches_only_its_explicit_primary_profile(kind):
    report = synthetic_report()
    policy = PromotionPolicy(source_id=SOURCE, source_contract_version="synthetic-v1",
        quantity_unit=QuantityUnit.SHARES)
    assert policy.matches(report)
    changed = {
        "source": replace(report, lineage=synthetic_lineage(source_id=OTHER)),
        "contract": replace(report, lineage=synthetic_lineage(source_contract_version="synthetic-v2")),
        "taxonomy": replace(report, taxonomy_version="synthetic-taxonomy-v2"),
        "unit": replace(report, quantity_unit=QuantityUnit.UNITS),
    }[kind]
    assert not policy.matches(changed)


def test_unsupported_promotion_policy_cannot_claim_a_verified_ordering():
    with pytest.raises(InstitutionalValidationError) as error:
        PromotionPolicy(source_id=SOURCE, source_contract_version="synthetic-v1",
            quantity_unit=QuantityUnit.SHARES, version="invented-provider-revision-v99")
    assert error.value.code == "unsupported_promotion_policy"
