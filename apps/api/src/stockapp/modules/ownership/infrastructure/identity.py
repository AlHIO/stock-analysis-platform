"""Reviewed dated bindings through market_data's public read-only boundary."""
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from uuid import UUID

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.ownership.domain.contracts import InstrumentBinding
from stockapp.modules.ownership.domain.models import OwnershipError
from stockapp.modules.ownership.domain.ingestion import json_value, binding_from_dict, FrozenIdentityResolver

FIELDS={'instrument_id','market_id','symbol','quantity_unit','valid_from','valid_through','evidence_version'}


def validate_evidence(entries):
    if not isinstance(entries,list) or len(entries)>20_000:
        raise OwnershipError('invalid_identity_evidence')
    result,seen=[],set()
    for entry in entries:
        if not isinstance(entry,dict) or not FIELDS <= entry.keys() or entry.keys()-FIELDS-{'source_reference'}:
            raise OwnershipError('invalid_identity_evidence')
        try:
            binding=binding_from_dict(entry)
            reference=entry.get('source_reference')
            if reference is not None and (not isinstance(reference,str) or not 1 <= len(reference) <= 2048 or '\x00' in reference):
                raise OwnershipError('invalid_identity_evidence')
        except (ValueError,TypeError,KeyError):
            raise OwnershipError('invalid_identity_evidence') from None
        if binding.symbol in seen:
            raise OwnershipError('ambiguous_identity_evidence')
        seen.add(binding.symbol)
        result.append(json_value(asdict(binding)) | ({'source_reference':reference} if reference else {}))
    return result


def load_identity_evidence(path:Path|None):
    if path is None:
        return []
    with path.open('rb') as stream:
        body=stream.read(5_000_001)
    if len(body)>5_000_000:
        raise OwnershipError('identity_evidence_too_large')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:
                raise OwnershipError('duplicate_evidence_key')
            result[key]=value
        return result
    try:
        doc=json.loads(body.decode('utf-8-sig'),object_pairs_hook=pairs)
        if (not isinstance(doc,dict) or set(doc)!={'version','synthetic','entries'}
            or doc['version']!='ownership-identity-evidence-v1' or doc['synthetic'] is not False):
            raise OwnershipError('unapproved_identity_evidence')
        return validate_evidence(doc['entries'])
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise OwnershipError('invalid_identity_evidence') from None


def snapshot_identities(session, *, symbols, as_of_date, evidence):
    reader=instrument_identity_reader(session)
    reviewed={entry['symbol']:entry for entry in validate_evidence(evidence)}
    result={}
    for symbol in sorted(set(symbols)):
        candidates=tuple(value for value in reader.find_by_symbol(symbol) if value.market_code in {'TWSE','TPEX'})
        entry=reviewed.get(symbol)
        if not candidates:
            value={'reason':'unknown_instrument'}
        elif len(candidates)!=1:
            value={'reason':'ambiguous_instrument'}
        elif entry is None:
            value={'reason':'historical_identity_unresolved'}
        else:
            binding=binding_from_dict(entry)
            known=candidates[0]
            if (binding.instrument_id,binding.market_id)!=(known.instrument_id,known.market_id):
                value={'reason':'instrument_identity_mismatch'}
            elif known.quantity_unit != binding.quantity_unit:
                value={'reason':'instrument_unit_mismatch'}
            elif not binding.valid_from <= as_of_date <= binding.valid_through:
                value={'reason':'historical_identity_unresolved'}
            else:
                value={'binding':json_value(asdict(binding))}
        result[symbol]=value
    return {'version':'ownership-identity-snapshot-v1','as_of_date':as_of_date.isoformat(),'symbols':result}

