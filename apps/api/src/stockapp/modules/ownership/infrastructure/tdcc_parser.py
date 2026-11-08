"""Strict six-column TDCC CSV parser. Whole-symbol groups never cross dates."""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import io
import re
from zoneinfo import ZoneInfo

from stockapp.modules.ownership.domain.contracts import (
    OwnershipIssue, ParsedBatch, ParsedOwnershipReport, RawPayload,
)
from stockapp.modules.ownership.domain.models import HoldingBucket, OwnershipError, TDCC_SCHEME
from .source_manifest import TDCC_CONTRACT_VERSION, TDCC_ENDPOINT, TDCC_FIELDS, TDCC_PARAMETERS, PARSER_VERSION

_BIGINT_MAX = 2**63 - 1
_CSV_TYPES = {"text/csv", "application/csv", "text/plain", "application/octet-stream"}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)")
_PERCENT = re.compile(r"[+-]?(?:[0-9]+)(?:\.[0-9]{1,12})?")


def _number(value):
    if value.strip() in {"", "--"}:
        return None
    value = value.strip()
    if len(value) > 32 or not _NUMBER.fullmatch(value):
        raise OwnershipError("invalid_integer_quantity")
    result = int(value.replace(",", ""))
    if not -2**63 <= result <= _BIGINT_MAX:
        raise OwnershipError("quantity_out_of_range")
    return result


def _percent(value):
    value = value.strip()
    if value in {"", "--"}:
        return None
    if len(value) > 22 or not _PERCENT.fullmatch(value):
        raise OwnershipError("invalid_reported_percent")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise OwnershipError("invalid_reported_percent") from None
    if not result.is_finite() or abs(result) > Decimal('99999999.999999999999'):
        raise OwnershipError("invalid_reported_percent")
    return result


def _day(value):
    value = value.strip()
    if not re.fullmatch(r"[0-9]{8}", value):
        raise OwnershipError("invalid_as_of_date")
    try:
        result = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        raise OwnershipError("invalid_as_of_date") from None
    return result


def _decode(raw):
    parts = [part.strip() for part in raw.content_type.lower().split(";")]
    if parts[0] not in _CSV_TYPES:
        raise OwnershipError("unexpected_content_type")
    charsets = [part.split("=", 1)[1].strip('"') for part in parts[1:] if part.startswith("charset=")]
    if len(charsets) > 1:
        raise OwnershipError("ambiguous_encoding")
    charset = charsets[0] if charsets else "utf-8"
    # UTF-8 is the audited O1 representation. A declared Big5 transport may
    # decode the same exact six-column contract; never guess fallback encodings.
    encodings = {"utf-8":"utf-8-sig", "utf8":"utf-8-sig", "utf-8-sig":"utf-8-sig",
        "big5":"cp950", "cp950":"cp950"}
    if charset not in encodings:
        raise OwnershipError("unsupported_encoding")
    try:
        return raw.body.decode(encodings[charset])
    except UnicodeError:
        raise OwnershipError("invalid_csv_encoding") from None


def parse_tdcc_csv(raw: RawPayload) -> ParsedBatch:
    if raw.status_code != 200:
        raise OwnershipError("unsuccessful_raw_response")
    if raw.request_method != 'GET' or raw.endpoint != TDCC_ENDPOINT or raw.request_parameters != TDCC_PARAMETERS:
        raise OwnershipError("unexpected_source_context")
    reader = csv.reader(io.StringIO(_decode(raw), newline=""), strict=True)
    try:
        header = next(reader)
        fields = [value.strip() for value in header]
        if fields and fields[0].startswith("\ufeff"):
            fields[0] = fields[0].removeprefix("\ufeff")
        if len(fields) != len(set(fields)):
            raise OwnershipError("duplicate_columns")
        if set(fields) != set(TDCC_FIELDS) or len(fields) != len(TDCC_FIELDS):
            raise OwnershipError("unknown_csv_schema")
        groups, dates = {}, set()
        row_count = 0
        for index, values in enumerate(reader):
            if not values:
                continue  # An empty physical CSV line is not a security observation.
            if len(values) != len(fields):
                raise OwnershipError("invalid_csv_row_width")
            row = dict(zip(fields, values, strict=True))
            day = _day(row["資料日期"])
            dates.add(day)
            if len(dates) > 1:
                raise OwnershipError("mixed_as_of_dates")
            if day > raw.fetched_at.astimezone(ZoneInfo("Asia/Taipei")).date():
                raise OwnershipError("future_as_of_date")
            symbol = row["證券代號"].strip()
            if not symbol or len(symbol) > 32 or not re.fullmatch(r"[A-Za-z0-9]+", symbol):
                # No safe group identity means the envelope cannot be accounted
                # to a report. Preserve the raw and fail instead of dropping it.
                raise OwnershipError("invalid_source_symbol")
            groups.setdefault((symbol, day), []).append((index, row))
            row_count += 1
            if row_count > 200_000:
                raise OwnershipError("too_many_source_rows")
    except (StopIteration, csv.Error):
        raise OwnershipError("invalid_csv_response") from None
    if not groups:
        raise OwnershipError("empty_response")
    definitions = {definition.code: definition for definition in TDCC_SCHEME.definitions}
    reports, issues = [], []
    for (symbol, day), rows in sorted(groups.items()):
        indices = tuple(index for index, _ in rows)
        raw_rows = tuple(row for _, row in rows)
        try:
            buckets, seen = [], set()
            for _, row in rows:
                code = row["持股分級"].strip()
                if code not in definitions:
                    raise OwnershipError("unknown_bucket_code")
                if code in seen:
                    raise OwnershipError("duplicate_bucket")
                seen.add(code)
                buckets.append(HoldingBucket(code=code, kind=definitions[code].kind,
                    holder_count=_number(row["人數"]), quantity=_number(row["股數"]),
                    reported_percent=_percent(row["占集保庫存數比例%"])))
            reports.append(ParsedOwnershipReport(symbol=symbol, as_of_date=day,
                buckets=tuple(sorted(buckets, key=lambda bucket:int(bucket.code))),
                source_rows=indices, raw_rows=raw_rows))
        except OwnershipError as exc:
            issues.append(OwnershipIssue(symbol=symbol, as_of_date=day, reason=exc.code,
                source_rows=indices, raw_rows=raw_rows))
    return ParsedBatch(raw_id=raw.id, raw_sha256=raw.sha256, scheme=TDCC_SCHEME,
        reports=tuple(reports), issues=tuple(issues), source_contract_version=TDCC_CONTRACT_VERSION,
        parser_version=PARSER_VERSION)
