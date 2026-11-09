"""Reviewed identity and lot evidence through market_data's public reader."""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from stockapp.modules.margin.domain.ingestion import MarginInstrumentBinding
from stockapp.modules.margin.domain.models import (
    ConversionEvidence, DateEvidence, MarginError, QuantityUnit, SourceTiming,
)

_ENTRY_FIELDS = {'symbol','market_code','instrument_id','market_id','quantity_unit',
    'valid_from','valid_through','evidence_version','source_reference','conversion'}
_CONVERSION_FIELDS = {'source_unit','target_unit','factor','valid_from','valid_through',
    'evidence_version','source_reference'}
_DATE_FIELDS = {'payload_sha256','trade_date','evidence_version','source_reference'}


def _reference(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise MarginError('invalid_evidence_reference')
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.fragment or not any(url.hostname == host or url.hostname.endswith('.'+host)
                   for host in ('twse.com.tw','tpex.org.tw','data.gov.tw'))):
            raise ValueError('unreviewed_reference')
    except ValueError:
        raise MarginError('invalid_evidence_reference') from None


def binding_from_dict(entry):
    try:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise MarginError('invalid_margin_evidence_entry')
        _reference(entry['source_reference'])
        conversion = entry['conversion']
        if not isinstance(conversion, dict) or set(conversion) != _CONVERSION_FIELDS:
            raise MarginError('invalid_margin_conversion_evidence')
        _reference(conversion['source_reference'])
        if not isinstance(conversion['factor'], str):
            raise MarginError('conversion_factor_must_be_decimal_string')
        instrument_id, market_id = UUID(entry['instrument_id']), UUID(entry['market_id'])
        return MarginInstrumentBinding(instrument_id=instrument_id, market_id=market_id,
            market_code=entry['market_code'], symbol=entry['symbol'],
            quantity_unit=QuantityUnit(entry['quantity_unit']),
            valid_from=date.fromisoformat(entry['valid_from']),
            valid_through=date.fromisoformat(entry['valid_through']),
            evidence_version=entry['evidence_version'],
            conversion=ConversionEvidence(instrument_id=instrument_id, market_id=market_id,
                source_unit=QuantityUnit(conversion['source_unit']),
                target_unit=QuantityUnit(conversion['target_unit']),
                factor=Decimal(conversion['factor']),
                valid_from=date.fromisoformat(conversion['valid_from']),
                valid_through=date.fromisoformat(conversion['valid_through']),
                evidence_version=conversion['evidence_version']))
    except MarginError:
        raise
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise MarginError('invalid_margin_evidence_entry') from None


def validate_margin_evidence(doc):
    if (not isinstance(doc, dict) or set(doc)-{'version','synthetic','entries','date_evidence'}
            or not {'version','synthetic','entries'} <= set(doc)
            or doc['version'] != 'margin-evidence-v1' or doc['synthetic'] is not False
            or not isinstance(doc['entries'], list) or len(doc['entries']) > 20_000):
        raise MarginError('unapproved_margin_evidence')
    seen = {}
    for entry in doc['entries']:
        binding = binding_from_dict(entry)
        key = binding.market_code, binding.symbol
        intervals = seen.setdefault(key, [])
        if any(binding.valid_from <= end and start <= binding.valid_through for start,end in intervals):
            raise MarginError('ambiguous_margin_identity_evidence')
        intervals.append((binding.valid_from,binding.valid_through))
    proof = doc.get('date_evidence')
    if proof is not None:
        if not isinstance(proof, dict) or set(proof) != _DATE_FIELDS:
            raise MarginError('invalid_margin_date_evidence')
        _reference(proof['source_reference'])
        try:
            date.fromisoformat(proof['trade_date'])
        except (ValueError,TypeError):
            raise MarginError('invalid_margin_date_evidence') from None
        # SourceTiming validates exact SHA256 and evidence text when bound to raw.
        if (not isinstance(proof['payload_sha256'], str) or len(proof['payload_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in proof['payload_sha256'])
                or not isinstance(proof['evidence_version'], str) or not proof['evidence_version'].strip()):
            raise MarginError('invalid_margin_date_evidence')
    return json.loads(json.dumps(doc, allow_nan=False))


def load_margin_evidence(path: Path | None):
    if path is None:
        return {'version':'margin-evidence-v1','synthetic':False,'entries':[]}
    with Path(path).open('rb') as stream:
        body = stream.read(5_000_001)
    if len(body) > 5_000_000:
        raise MarginError('margin_evidence_too_large')
    def pairs(values):
        result = {}
        for key,value in values:
            if key in result:
                raise MarginError('duplicate_evidence_key')
            result[key] = value
        return result
    try:
        return validate_margin_evidence(json.loads(body.decode('utf-8-sig'), object_pairs_hook=pairs))
    except MarginError:
        raise
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise MarginError('invalid_margin_evidence') from None


def date_timing_from_evidence(raw, doc):
    proof = doc.get('date_evidence')
    if proof is None:
        return None
    if proof['payload_sha256'] != raw.sha256:
        raise MarginError('source_date_evidence_mismatch')
    return SourceTiming(trade_date=date.fromisoformat(proof['trade_date']),
        date_evidence=DateEvidence.VERIFIED_PAYLOAD,
        date_evidence_version=proof['evidence_version'], first_seen_at=raw.fetched_at,
        date_evidence_payload_sha256=raw.sha256)


class ReviewedMarginResolver:
    """A current UUID match confirms identity only alongside reviewed dates."""
    def __init__(self, reader, evidence):
        self.reader = reader
        self.evidence = validate_margin_evidence(evidence)
        self.bindings = tuple(binding_from_dict(entry) for entry in self.evidence['entries'])
        self.cache = {}

    def resolve(self, *, symbol, trade_date, market_code):
        bindings = [binding for binding in self.bindings
                    if binding.symbol == symbol and binding.market_code == market_code
                    and binding.valid_from <= trade_date <= binding.valid_through]
        if not bindings:
            raise MarginError('historical_identity_unresolved')
        if len(bindings) != 1:
            raise MarginError('ambiguous_margin_identity_evidence')
        binding, = bindings
        if symbol not in self.cache:
            self.cache[symbol] = tuple(self.reader.find_by_symbol(symbol))
        candidates = [known for known in self.cache[symbol] if known.market_code == market_code]
        if not candidates:
            raise MarginError('unknown_instrument')
        if len(candidates) != 1:
            raise MarginError('ambiguous_instrument')
        known = candidates[0]
        if (known.instrument_id,known.market_id) != (binding.instrument_id,binding.market_id):
            raise MarginError('instrument_identity_mismatch')
        if known.quantity_unit != binding.quantity_unit.value:
            raise MarginError('instrument_unit_mismatch')
        if not binding.conversion.valid_from <= trade_date <= binding.conversion.valid_through:
            raise MarginError('conversion_evidence_not_applicable')
        return binding
