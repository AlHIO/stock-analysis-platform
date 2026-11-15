"""Strict, versioned platform CSV parser for already archived offline bytes.

This interchange contract is deliberately distinct from unverified provider
formats. It parses named fields as text; domain normalization owns all dates,
quantity conversion, exact decimals and whole-report validation.
"""
import csv
from hashlib import sha256
import io
from uuid import UUID

from ..domain.ingestion import (
    CSV_COLUMNS, OFFLINE_PARSER_VERSION, OFFLINE_SCHEMA_VERSION,
    ParsedSourceBatch, SourceRow,
)
from ..domain.models import BrokerFlowError
from .files import DEFAULT_LIMITS, FileLimits, csv_members


MAX_ROWS = 100_000
MAX_FIELD_CHARACTERS = 16_384
MAX_PHYSICAL_LINE_CHARACTERS = 524_288
MAX_HEADER_CHARACTERS = 4_096


class _BoundedLines:
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_PHYSICAL_LINE_CHARACTERS + 1)
        if not line:
            raise StopIteration
        if len(line) > MAX_PHYSICAL_LINE_CHARACTERS:
            raise BrokerFlowError("broker_csv_line_too_large")
        return line


def _locator(member_name: str, line: int) -> str:
    locator = f"csv:{member_name}:line={line}"
    if len(locator) > 256:
        locator = f"csv-member-sha256:{sha256(member_name.encode('utf-8')).hexdigest()}:line={line}"
    return locator


def parse_csv_rows(body: bytes, member_name: str, *, encoding: str = "utf-8",
                   start_index: int = 0, max_rows: int = MAX_ROWS) -> tuple[SourceRow, ...]:
    """Parse one member with deterministic locators and per-row shape issues."""
    if encoding not in {"utf-8", "big5"}:
        raise BrokerFlowError("unsupported_broker_csv_encoding")
    if (type(start_index) is not int or start_index < 0 or type(max_rows) is not int
            or max_rows <= 0 or max_rows > MAX_ROWS):
        raise BrokerFlowError("invalid_broker_parser_limits")
    if not isinstance(body, bytes):
        raise BrokerFlowError("invalid_broker_csv_body")
    try:
        decoded = body.decode("utf-8-sig" if encoding == "utf-8" else "big5", errors="strict")
    except UnicodeError:
        raise BrokerFlowError("invalid_broker_csv_encoding") from None
    if "\x00" in decoded:
        raise BrokerFlowError("invalid_broker_csv_encoding")
    stream = io.StringIO(decoded, newline="")
    header_line = stream.readline(MAX_HEADER_CHARACTERS + 1)
    if not header_line:
        raise BrokerFlowError("empty_broker_csv")
    if len(header_line) > MAX_HEADER_CHARACTERS:
        raise BrokerFlowError("broker_csv_header_too_large")
    try:
        header = next(csv.reader([header_line], strict=True))
        if len(set(header)) != len(header):
            raise BrokerFlowError("duplicate_broker_csv_columns")
        if set(header) != set(CSV_COLUMNS) or len(header) != len(CSV_COLUMNS):
            raise BrokerFlowError("unsupported_broker_csv_columns")
        reader = csv.reader(_BoundedLines(stream), strict=True)
        result = []
        while True:
            line = reader.line_num + 2  # The physical header occupies line 1.
            try:
                cells = next(reader)
            except StopIteration:
                break
            if len(result) >= max_rows:
                raise BrokerFlowError("broker_csv_row_limit")
            if any(len(cell) > MAX_FIELD_CHARACTERS for cell in cells):
                raise BrokerFlowError("broker_csv_field_too_large")
            fields = tuple((key, cells[index] if index < len(cells) else "")
                           for index, key in enumerate(header))
            values = dict(fields)
            if values["schema_version"] != OFFLINE_SCHEMA_VERSION:
                raise BrokerFlowError("unsupported_broker_csv_schema")
            issue = "csv_column_count_mismatch" if len(cells) != len(header) else None
            result.append(SourceRow(row_index=start_index+len(result),
                row_locator=_locator(member_name, line), fields=fields, issue_code=issue))
        if not result:
            raise BrokerFlowError("empty_broker_csv")
        return tuple(result)
    except BrokerFlowError:
        raise
    except (csv.Error, UnicodeError, ValueError):
        raise BrokerFlowError("malformed_broker_csv") from None


def parse_archive(body: bytes, filename: str, *, raw_id: UUID, raw_sha256: str,
                  encoding: str = "utf-8", limits: FileLimits = DEFAULT_LIMITS) -> ParsedSourceBatch:
    """Decode an immutable Raw Archive only after its bytes have been committed."""
    if not isinstance(body, bytes) or sha256(body).hexdigest() != raw_sha256:
        raise BrokerFlowError("broker_archive_hash_mismatch")
    members = csv_members(body, filename, limits=limits)
    rows = []
    for member in members:
        remaining = MAX_ROWS - len(rows)
        if remaining <= 0:
            raise BrokerFlowError("broker_csv_row_limit")
        rows.extend(parse_csv_rows(member.body, member.name, encoding=encoding,
                                   start_index=len(rows), max_rows=remaining))
    return ParsedSourceBatch(raw_id=raw_id, raw_sha256=raw_sha256,
        schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION,
        rows=tuple(rows))
