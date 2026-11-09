"""Reviewed official margin sources; offline parsing never enables downloading."""
from datetime import date
from decimal import Decimal
import json
import re

from stockapp.modules.margin.domain.ingestion import (
    MarginSourceError, ParsedBatch, ParsedMarginRow, RowIssue,
)
from stockapp.modules.margin.domain.models import (
    Availability, DateEvidence, MarginError, Quantity, QuantityUnit, SourceTiming,
)
from .source_manifest import TPEX_MANIFEST, TWSE_DATED_MANIFEST, TWSE_OPENAPI_MANIFEST
from .transport import MarginHttpTransport

PARSER_VERSION = 'margin-openapi-parser-1.0.0'
_NUMERIC = re.compile(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\Z')
_QUANTITIES = {
    'financing.previous_balance', 'financing.purchase', 'financing.sale',
    'financing.cash_repayment', 'financing.balance', 'financing.limit',
    'short_margin.previous_balance', 'short_margin.short_sell',
    'short_margin.short_cover', 'short_margin.stock_repayment',
    'short_margin.balance', 'short_margin.limit',
}


class _SourceObject(dict):
    def __init__(self, pairs):
        super().__init__()
        self.duplicate_names = []
        for key, value in pairs:
            if key in self:
                self.duplicate_names.append(key)
            self[key] = value


def parse_quantity(value, *, unit=QuantityUnit.LOTS):
    """Preserve an explicit source zero; ambiguous sentinels stay missing."""
    if value is None or isinstance(value, str) and value.strip() in {'', '--', '---', '-'}:
        return Quantity(value=None, unit=unit, availability=Availability.MISSING)
    if isinstance(value, str) and value.strip() in {'不適用', 'N/A'}:
        return Quantity(value=None, unit=unit, availability=Availability.NOT_APPLICABLE)
    if type(value) is int:
        number = value
    elif isinstance(value, str) and len(value) <= 64 and _NUMERIC.fullmatch(value.strip()):
        number = int(value.strip().replace(',', ''))
    else:
        raise MarginError('invalid_source_quantity')
    if number < 0:
        raise MarginError('negative_balance_or_flow')
    return Quantity(value=number, unit=unit, availability=Availability.REPORTED)


def parse_source_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{7}', value):
        raise MarginError('invalid_source_trade_date')
    try:
        return date(int(value[:3])+1911, int(value[3:5]), int(value[5:7]))
    except ValueError:
        raise MarginError('invalid_source_trade_date') from None


def parse_margin(raw, manifest, *, date_evidence=None):
    if raw.status_code != 200:
        raise MarginSourceError(f'http_{raw.status_code}', raw)
    if raw.source_contract_version != manifest.contract.version:
        raise MarginSourceError('source_contract_version_mismatch', raw)
    if not manifest.contract.column_contract_verified:
        raise MarginSourceError('source_column_contract_not_verified', raw)
    try:
        doc = json.loads(raw.body.decode('utf-8-sig'), object_pairs_hook=_SourceObject,
                         parse_float=Decimal,
                         parse_constant=lambda value:(_ for _ in ()).throw(ValueError('nonfinite_json')))
    except (ValueError, UnicodeError, RecursionError):
        raise MarginSourceError('unexpected_response', raw) from None
    if not isinstance(doc, list) or len(doc) > 100_000:
        raise MarginSourceError('unexpected_response', raw)
    rows, issues = [], []
    expected_date = None
    if date_evidence is not None:
        if not isinstance(date_evidence, SourceTiming):
            raise MarginSourceError('invalid_source_date_evidence', raw)
        if (date_evidence.date_evidence is not DateEvidence.VERIFIED_PAYLOAD
                or date_evidence.date_evidence_payload_sha256 != raw.sha256
                or manifest.contract.date_blockers(date_evidence)):
            raise MarginSourceError('source_date_evidence_mismatch', raw)
        expected_date = date_evidence.trade_date
    for index, entry in enumerate(doc):
        symbol, day = None, None
        raw_row = dict(entry) if isinstance(entry, dict) else {'unparsed':str(entry)[:500]}
        try:
            if not isinstance(entry, dict):
                raise MarginError('invalid_source_row')
            if getattr(entry, 'duplicate_names', ()):
                raise MarginError('duplicate_source_columns')
            manifest.contract.validate_columns(tuple(entry))
            mapped = {field.domain_name:entry[field.source_name] for field in manifest.contract.fields}
            symbol, name = mapped['symbol'], mapped['name']
            if (not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9]{1,16}', symbol)
                    or not isinstance(name, str) or not name.strip() or len(name) > 200):
                raise MarginError('invalid_source_identity')
            if manifest.contract.date_field is not None:
                day = parse_source_date(mapped['trade_date'])
            else:
                day = expected_date
            if day is not None:
                timing = SourceTiming(trade_date=day,
                    date_evidence=DateEvidence.SOURCE_FIELD if manifest.contract.date_field else DateEvidence.VERIFIED_PAYLOAD,
                    date_evidence_version=manifest.contract.version if manifest.contract.date_field else date_evidence.date_evidence_version,
                    date_evidence_payload_sha256=None if manifest.contract.date_field else raw.sha256,
                    first_seen_at=raw.fetched_at)
                if manifest.contract.date_blockers(timing):
                    raise MarginError('source_trade_date_not_verified')
            quantities = tuple((name, parse_quantity(mapped[name], unit=manifest.contract.quantity_unit))
                               for name in sorted(_QUANTITIES))
            for field in manifest.contract.fields:
                extra = mapped[field.domain_name]
                if field.unit == 'lots' and field.domain_name not in _QUANTITIES:
                    parse_quantity(extra, unit=manifest.contract.quantity_unit)
                elif field.unit == 'percent' and extra is not None and extra not in ('','--','N/A','不適用'):
                    if (not isinstance(extra, str) or len(extra) > 64
                            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', extra.strip())):
                        raise MarginError('invalid_source_percentage')
                    number = Decimal(extra.strip())
                    if not number.is_finite() or number < 0 or number.adjusted() >= 8 or max(-number.as_tuple().exponent,0) > 12:
                        raise MarginError('invalid_source_percentage')
                elif field.domain_name == 'note' and (not isinstance(extra, str) or len(extra) > 2000):
                    raise MarginError('invalid_source_note')
            rows.append(ParsedMarginRow(row_index=index, symbol=symbol, name=name,
                        trade_date=day, quantities=quantities, raw_row=raw_row))
        except MarginError as error:
            issues.append(RowIssue(row_index=index, code=error.code, symbol=symbol,
                                  trade_date=day, raw_row=raw_row))
    # Multiple rows for one instrument in a single official snapshot are
    # ambiguous even if numbers happen to agree. Neither row wins by position.
    counts = {}
    for row in rows:
        counts[row.symbol] = counts.get(row.symbol, 0) + 1
    unique_rows = []
    mixed_dates = len({row.trade_date for row in rows if row.trade_date is not None}) > 1
    for row in rows:
        code = 'mixed_source_trade_dates' if mixed_dates else 'duplicate_source_symbol' if counts[row.symbol] > 1 else None
        if code:
            issues.append(RowIssue(row_index=row.row_index, code=code, symbol=row.symbol,
                                  trade_date=row.trade_date, raw_row=row.raw_row))
        else:
            unique_rows.append(row)
    if not doc:
        issues.append(RowIssue(row_index=0, code='empty_source_response', raw_row={}))
    return ParsedBatch(raw_id=raw.id, raw_sha256=raw.sha256, contract=manifest.contract,
                       parser_version=PARSER_VERSION, rows=tuple(unique_rows),
                       issues=tuple(sorted(issues, key=lambda issue:issue.row_index)))


class _MarginAdapter:
    def __init__(self, *, network_enabled=False, transport=None):
        if type(network_enabled) is not bool:
            raise ValueError('network_enabled_must_be_boolean')
        self.network_enabled = network_enabled
        self.transport, self.owned = transport, transport is None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self.owned and self.transport is not None:
            await self.transport.aclose()

    async def fetch(self, *, run_id, source_id, trade_date=None, on_response=None):
        if self.manifest.authorization not in {'ogdl_dataset_documented', 'ogdl_platform_documented'}:
            raise MarginSourceError('source_authorization_pending')
        self.manifest.contract.validate_request(trade_date)
        if not self.network_enabled:
            raise MarginSourceError('network_disabled')
        if self.transport is None:
            self.transport = MarginHttpTransport()
        return await self.transport.fetch(endpoint=self.manifest.endpoint, parameters={},
            run_id=run_id, source_id=source_id, source_contract_version=self.manifest.contract.version,
            on_response=on_response)

    def parse(self, raw, *, date_evidence=None):
        return parse_margin(raw, self.manifest, date_evidence=date_evidence)


class TpexMarginAdapter(_MarginAdapter):
    manifest = TPEX_MANIFEST


class TwseMarginAdapter(_MarginAdapter):
    manifest = TWSE_OPENAPI_MANIFEST


class TwseDatedMarginAdapter(_MarginAdapter):
    """A capability gate only; M2 has no approved downloader or parser."""
    manifest = TWSE_DATED_MANIFEST
