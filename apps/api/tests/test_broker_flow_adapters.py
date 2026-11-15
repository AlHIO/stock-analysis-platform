"""Fixed SYNTHETIC interchange fixtures, never licensed provider reports."""
import csv
from hashlib import sha256
import io
from pathlib import Path
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.domain.ingestion import (
    CSV_COLUMNS, OFFLINE_PARSER_VERSION, OFFLINE_SCHEMA_VERSION,
)
from stockapp.modules.broker_flow.domain.models import BrokerFlowError
from stockapp.modules.broker_flow.infrastructure.parsers import parse_archive, parse_csv_rows
from test_broker_flow_files import synthetic_zip


FIXTURE = Path(__file__).parent / "fixtures/broker_flow/SYNTHETIC-offline-v1.csv"
SYNTHETIC_RAW_ID = UUID(int=90210)


def fixture_rows():
    with FIXTURE.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def encode_rows(rows=None, *, columns=CSV_COLUMNS, encoding="utf-8", bom=False):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in fixture_rows() if rows is None else rows:
        writer.writerow([row.get(column, "") for column in columns])
    return (b"\xef\xbb\xbf" if bom else b"") + out.getvalue().encode(encoding)


def parse(body, filename="SYNTHETIC.csv", **kwargs):
    return parse_archive(body, filename, raw_id=SYNTHETIC_RAW_ID,
                         raw_sha256=sha256(body).hexdigest(), **kwargs)


def test_fixed_fixture_preserves_original_fields_and_numbers_as_text():
    body = FIXTURE.read_bytes()
    batch = parse(body)
    assert batch.raw_id == SYNTHETIC_RAW_ID and batch.raw_sha256 == sha256(body).hexdigest()
    assert batch.schema_version == OFFLINE_SCHEMA_VERSION and batch.parser_version == OFFLINE_PARSER_VERSION
    assert len(batch.rows) == 1 and batch.rows[0].row_index == 0
    row = batch.rows[0]
    assert row.row_locator == "csv:SYNTHETIC.csv:line=2" and row.issue_code is None
    assert row.values["report_id"] == "SYNTHETIC-合成報表"
    assert row.values["branch_code"] == "00Aa"
    assert row.values["buy_quantity"] == "1,000"
    assert row.values["price"] == "0.10000001"
    assert row.values["buy_amount"] == "100.000010000000"
    assert row.values["source_published_at"] == ""


@pytest.mark.parametrize("encoding,bom", [("utf-8", False), ("utf-8", True), ("big5", False)])
def test_explicit_encoding_and_utf8_bom_preserve_same_text(encoding, bom):
    body = encode_rows(encoding=encoding, bom=bom)
    assert parse(body, encoding=encoding).rows[0].values == fixture_rows()[0]


def test_encoding_is_never_guessed_or_silently_replaced():
    with pytest.raises(BrokerFlowError, match="invalid_broker_csv_encoding"):
        parse(encode_rows(encoding="big5"), encoding="utf-8")
    for encoding in ("cp950", "latin-1", "utf-16", "auto"):
        with pytest.raises(BrokerFlowError, match="unsupported_broker_csv_encoding"):
            parse(FIXTURE.read_bytes(), encoding=encoding)


def test_hash_mismatch_cannot_relabel_the_raw_archive():
    with pytest.raises(BrokerFlowError, match="broker_archive_hash_mismatch"):
        parse_archive(FIXTURE.read_bytes(), "SYNTHETIC.csv", raw_id=SYNTHETIC_RAW_ID,
                      raw_sha256="a" * 64)


def test_columns_are_selected_by_exact_name_instead_of_position():
    reordered = tuple(reversed(CSV_COLUMNS))
    assert parse(encode_rows(columns=reordered)).rows[0].values == fixture_rows()[0]


@pytest.mark.parametrize("columns,reason", [
    (CSV_COLUMNS + ("unknown_provider_field",), "unsupported_broker_csv_columns"),
    (CSV_COLUMNS[:-1], "unsupported_broker_csv_columns"),
    (CSV_COLUMNS[:-1] + (CSV_COLUMNS[0],), "duplicate_broker_csv_columns"),
    ((" schema_version",) + CSV_COLUMNS[1:], "unsupported_broker_csv_columns"),
])
def test_unknown_missing_duplicate_and_whitespace_headers_rejected(columns, reason):
    with pytest.raises(BrokerFlowError, match=reason):
        parse(encode_rows(columns=columns))


@pytest.mark.parametrize("schema", ["", "TWSE-guessed-v1", "stockapp.broker-flow.offline.v2"])
def test_unrecognized_or_missing_report_version_rejected(schema):
    row = fixture_rows()[0] | {"schema_version": schema}
    with pytest.raises(BrokerFlowError, match="unsupported_broker_csv_schema"):
        parse(encode_rows([row]))


@pytest.mark.parametrize("extra", [False, True])
def test_wrong_row_width_keeps_report_key_for_whole_report_audit(extra):
    body = encode_rows().decode()
    lines = list(csv.reader(io.StringIO(body)))
    if extra:
        lines[1].append("SYNTHETIC-extra-unlabelled-value")
    else:
        lines[1].pop()
    out = io.StringIO(newline="")
    csv.writer(out).writerows(lines)
    row = parse(out.getvalue().encode()).rows[0]
    assert row.issue_code == "csv_column_count_mismatch"
    assert row.values["report_id"] == fixture_rows()[0]["report_id"]
    assert row.values["instrument_symbol"] == "SYNTHETIC1"
    assert row.values["row_id"] == (fixture_rows()[0]["row_id"] if extra else "")


def test_invalid_financial_text_is_retained_for_domain_validation():
    row = fixture_rows()[0] | {"buy_quantity": "NaN", "price": "0.000000000000000000001",
                            "sell_amount": "Infinity", "reported_net_quantity": "-1.25"}
    parsed = parse(encode_rows([row])).rows[0]
    assert parsed.values == row and parsed.issue_code is None
    assert all(isinstance(value, str) for value in parsed.values.values())


def test_price_bucket_rows_are_not_mistaken_for_duplicate_branches():
    row = fixture_rows()[0]
    batch = parse(encode_rows([row, row | {"row_id": "SYNTHETIC-price-bucket-2", "price": "12"}]))
    assert len(batch.rows) == 2 and batch.rows[0].values["branch_code"] == batch.rows[1].values["branch_code"]
    assert [row.row_locator for row in batch.rows] == ["csv:SYNTHETIC.csv:line=2", "csv:SYNTHETIC.csv:line=3"]


def test_missing_quantity_and_explicit_zero_remain_different():
    row = fixture_rows()[0] | {"buy_quantity": "", "sell_quantity": "0", "reported_net_quantity": ""}
    values = parse(encode_rows([row])).rows[0].values
    assert values["buy_quantity"] == "" and values["sell_quantity"] == "0"


def test_zip_members_have_stable_global_indices_regardless_of_container_order():
    a = encode_rows([fixture_rows()[0] | {"row_id": "SYNTHETIC-a"}])
    z = encode_rows([fixture_rows()[0] | {"row_id": "SYNTHETIC-z"}])
    first = parse(synthetic_zip((("z.csv", z), ("a.csv", a))), "SYNTHETIC.zip")
    second = parse(synthetic_zip((("a.csv", a), ("z.csv", z))), "SYNTHETIC.zip")
    assert first.rows == second.rows
    assert [row.row_index for row in first.rows] == [0, 1]
    assert [row.row_locator for row in first.rows] == ["csv:a.csv:line=2", "csv:z.csv:line=2"]


def test_csv_quoted_newlines_keep_precise_physical_line_locators():
    original = fixture_rows()[0]
    first = original | {"report_id": "SYNTHETIC\nmultiline"}
    batch = parse(encode_rows([first, original | {"row_id": "SYNTHETIC-second"}]))
    assert [row.row_locator for row in batch.rows] == ["csv:SYNTHETIC.csv:line=2", "csv:SYNTHETIC.csv:line=4"]


@pytest.mark.parametrize("body,reason", [(b"", "empty_broker_csv"),
    (encode_rows([]), "empty_broker_csv"), (b"\xff", "invalid_broker_csv_encoding"),
    (encode_rows() + b'"SYNTHETIC-unterminated', "malformed_broker_csv")])
def test_empty_truncated_and_invalid_csv_are_explicit_failures(body, reason):
    with pytest.raises(BrokerFlowError, match=reason):
        parse(body)


def test_member_row_count_bound_is_enforced():
    row = fixture_rows()[0]
    with pytest.raises(BrokerFlowError, match="broker_csv_row_limit"):
        parse_csv_rows(encode_rows([row, row]), "SYNTHETIC.csv", max_rows=1)


def test_field_and_header_resource_limits_are_enforced():
    row = fixture_rows()[0] | {"row_id": "S" * 16_385}
    with pytest.raises(BrokerFlowError, match="broker_csv_field_too_large"):
        parse(encode_rows([row]))
    with pytest.raises(BrokerFlowError, match="broker_csv_header_too_large"):
        parse(b"S" * 4097 + b"\n")


def test_long_safe_member_names_use_a_reproducible_bounded_locator():
    name = "d/" * 120 + "SYNTHETIC.csv"
    body = synthetic_zip(((name, encode_rows()),))
    row = parse(body, "SYNTHETIC.zip").rows[0]
    assert row.row_locator == f"csv-member-sha256:{sha256(name.encode()).hexdigest()}:line=2"
    assert len(row.row_locator) <= 256
