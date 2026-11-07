"""Strict wire-format utilities. Financial reconciliation stays in Domain."""
from datetime import date
import json
import re

from stockapp.modules.institutional.domain.models import BIGINT_MAX, BIGINT_MIN
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import (
    InstitutionalSourceError, ParsedBatch, ParsedRow, RowIssue,
)

__all__ = ("InstitutionalSourceError", "ParsedBatch", "ParsedRow", "RowIssue",
           "decode_json", "normalize_columns", "parse_date", "parse_quantity")


def decode_json(raw: RawPayload, *, expected_endpoint: str) -> object:
    if raw.endpoint != expected_endpoint or raw.request_method != "GET":
        raise InstitutionalSourceError("unexpected_source_endpoint", raw)
    if raw.status_code != 200:
        raise InstitutionalSourceError(f"http_{raw.status_code}", raw)
    content_type = raw.content_type.split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "text/json"}:
        raise InstitutionalSourceError("unexpected_content_type", raw)

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise InstitutionalSourceError("duplicate_columns", raw)
            result[key] = value
        return result

    def nonfinite(_):
        raise InstitutionalSourceError("invalid_json_number", raw)

    try:
        return json.loads(raw.body.decode("utf-8-sig"), object_pairs_hook=pairs,
                          parse_constant=nonfinite)
    except InstitutionalSourceError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InstitutionalSourceError("unexpected_response", raw) from exc


def normalize_columns(names: list[str]) -> list[str]:
    if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) and name.strip() for name in names):
        raise InstitutionalSourceError("invalid_columns")
    result = [name.strip() for name in names]
    if len(set(result)) != len(result):
        raise InstitutionalSourceError("duplicate_columns")
    return result


def parse_date(value, format: str) -> date:
    if not isinstance(value, str):
        raise InstitutionalSourceError("invalid_date")
    patterns = {
        "gregorian_compact": r"([0-9]{4})([0-9]{2})([0-9]{2})",
        "roc_compact": r"([0-9]{3})([0-9]{2})([0-9]{2})",
        "gregorian_slash": r"([0-9]{4})/([0-9]{2})/([0-9]{2})",
        "roc_slash": r"([0-9]{3})/([0-9]{2})/([0-9]{2})",
    }
    if format not in patterns:
        raise InstitutionalSourceError("unsupported_date_format")
    match = re.fullmatch(patterns[format], value.strip())
    if match is None:
        raise InstitutionalSourceError("invalid_date")
    year, month, day = map(int, match.groups())
    if format.startswith("roc"):
        if year < 1:
            raise InstitutionalSourceError("invalid_date")
        year += 1911
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise InstitutionalSourceError("invalid_date") from exc


def parse_quantity(value) -> int | None:
    """Wire counts are already shares; never multiply them by a lot size."""
    if value is None or isinstance(value, str) and value.strip() in {"", "--"}:
        return None
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value.strip()):
        unseparated = value.strip().replace(",", "")
        if len(unseparated.lstrip("+-")) > 19:
            raise InstitutionalSourceError("invalid_quantity")
        number = int(unseparated)
    else:
        raise InstitutionalSourceError("invalid_quantity")
    if not BIGINT_MIN <= number <= BIGINT_MAX:
        raise InstitutionalSourceError("invalid_quantity")
    return number
