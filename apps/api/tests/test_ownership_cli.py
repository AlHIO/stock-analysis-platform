"""SYNTHETIC ownership CLI and dated identity boundaries, without DB/network."""
import asyncio
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from stockapp import cli
from stockapp.modules.ownership.domain.ingestion import FrozenIdentityResolver
from stockapp.modules.ownership.domain.models import OwnershipError
from stockapp.modules.ownership.infrastructure import identity, operations


def parse(*args):
    return cli._parser().parse_args(['ownership',*args])


def test_latest_only_command_defaults_and_source_capability():
    args=parse('sync','latest')
    assert args.command=='ownership' and args.ownership_action=='sync'
    assert args.ownership_sync_mode=='latest' and args.identity_evidence is None
    source=operations.source_spec()
    assert source.approved and source.source_code=='TDCC_OWNERSHIP_CSV'
    assert source.config['capability']=='latest_only' and source.config['license']=='OGDL-1.0'
    assert source.config['default_network_enabled'] is False


@pytest.mark.parametrize('args',[
    ('sync',),('sync','date'),('sync','latest','--date','2026-11-06'),
    ('sync','latest','--market','TWSE'),('replay',),('replay','--raw','not-a-uuid'),
    ('status','--id','bad'),('status','--limit','0'),('status','--limit','101'),
    ('status','--limit','many'),
])
def test_invalid_cli_is_rejected_before_settings_or_engine(monkeypatch,args):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('Settings must not be read'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:pytest.fail('Engine must not be created'))
    with pytest.raises(SystemExit) as result:cli.main(['ownership',*args])
    assert result.value.code==2


@pytest.mark.parametrize('args',[('--help',),('sync','--help'),('sync','latest','--help'),
    ('replay','--help'),('status','--help')])
def test_cli_help_has_no_config_database_or_provider_io(monkeypatch,args):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('Help must not read Settings'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:pytest.fail('Help must not create engine'))
    with pytest.raises(SystemExit) as result:cli.main(['ownership',*args])
    assert result.value.code==0


def test_replay_selectors_and_frozen_resume_evidence_are_exclusive(monkeypatch):
    raw_id,run_id=uuid4(),uuid4()
    assert parse('replay','--raw',str(raw_id)).raw==raw_id
    assert parse('replay','--resume',str(run_id)).resume==run_id
    with pytest.raises(SystemExit):parse('replay','--raw',str(raw_id),'--resume',str(run_id))
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('Invalid resume must not read Settings'))
    with pytest.raises(SystemExit) as error:
        cli.main(['ownership','replay','--resume',str(run_id),'--identity-evidence','changed.json'])
    assert error.value.code==2


@pytest.mark.parametrize('status,expected',[('succeeded',0),('partial',2),('failed',2),('running',2)])
def test_sync_exit_codes_preserve_partial_and_failed_semantics(status,expected):
    assert operations.exit_code(parse('sync','latest'),[{'status':status}])==expected
    assert operations.exit_code(parse('sync','latest'),[])==2


def test_status_exit_code_describes_lookup_not_run_success():
    assert operations.exit_code(parse('status'),[])==0
    assert operations.exit_code(parse('status'),[{'status':'failed'}])==0
    assert operations.exit_code(parse('status','--id',str(uuid4())),[])==2


class SyntheticEngine:
    disposed=False
    def dispose(self):self.disposed=True


def setup_main(monkeypatch):
    engine=SyntheticEngine()
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:SimpleNamespace(database_url='synthetic-test-only'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:engine)
    monkeypatch.setattr(cli,'SqlAlchemyIngestionStore',lambda *args:pytest.fail('Ownership must not create Phase1 store'))
    return engine


def test_cli_dispatches_ownership_and_disposes_its_engine(monkeypatch,capsys):
    engine=setup_main(monkeypatch);calls=[]
    async def execute(actual,args,settings):
        calls.append((actual,args.ownership_action))
        return [{'status':'partial','stats':{'accepted':1,'unresolved':2}}]
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['ownership','sync','latest'])==2
    assert calls==[(engine,'sync')] and engine.disposed
    assert json.loads(capsys.readouterr().out)[0]['stats']=={'accepted':1,'unresolved':2}


@pytest.mark.parametrize('error,expected',[(RuntimeError('postgresql://private:password@host/db'),1),
    (OwnershipError('ownership_archive_not_found'),2)])
def test_cli_errors_do_not_expose_private_connections(monkeypatch,capsys,error,expected):
    engine=setup_main(monkeypatch)
    async def execute(*args):raise error
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['ownership','status'])==expected and engine.disposed
    output=capsys.readouterr().out
    assert 'password' not in output and 'postgresql' not in output
    if expected==2:assert json.loads(output)['code']=='ownership_archive_not_found'


def test_status_composition_never_constructs_provider_or_downloads(monkeypatch):
    calls=[];engine=object()
    class Store:
        def inspect(self,identifier,*,limit):calls.append((identifier,limit));return []
    monkeypatch.setattr(operations,'SqlAlchemyOwnershipIngestionStore',lambda actual,**kwargs:Store())
    monkeypatch.setattr(operations,'RuntimeProvider',lambda *args:pytest.fail('Status must not create provider'))
    args=parse('status','--limit','7')
    assert asyncio.run(operations.execute(engine,args,SimpleNamespace(ingestion_min_interval_seconds=5)))==[]
    assert calls==[(None,7)]


def evidence(**changes):
    return dict(instrument_id=str(uuid4()),market_id=str(uuid4()),symbol='SYNTH1',quantity_unit='shares',
        valid_from='2026-11-06',valid_through='2026-11-06',evidence_version='synthetic-reviewed-v1')|changes


def document(entries,**changes):
    return dict(version='ownership-identity-evidence-v1',synthetic=False,entries=entries)|changes


def test_evidence_loader_preserves_reviewed_metadata_without_inventing_dates(tmp_path):
    entry=evidence(source_reference='https://example.invalid/synthetic-reviewed-proof')
    target=tmp_path/'synthetic-evidence.json';target.write_text(json.dumps(document([entry])))
    assert identity.load_identity_evidence(target)==[entry]
    assert identity.load_identity_evidence(None)==[]


@pytest.mark.parametrize('data',[
    document([evidence()],synthetic=True),document([evidence()],version='unknown-v9'),
    document([evidence()],extra='unknown'),document([evidence(quantity_unit='lots')]),
    document([evidence(valid_from='2026-11-07',valid_through='2026-11-06')]),
    document([evidence(instrument_id='invalid-uuid')]),document([evidence(unknown_field=True)]),
    document([evidence(source_reference=123)]),
])
def test_unapproved_or_invalid_identity_evidence_is_rejected(tmp_path,data):
    target=tmp_path/'synthetic-invalid.json';target.write_text(json.dumps(data))
    with pytest.raises(OwnershipError):identity.load_identity_evidence(target)


def test_duplicate_json_keys_and_ambiguous_symbol_bindings_are_rejected(tmp_path):
    target=tmp_path/'duplicate.json'
    target.write_text('{"version":"ownership-identity-evidence-v1","synthetic":false,"entries":[],"entries":[]}')
    with pytest.raises(OwnershipError):identity.load_identity_evidence(target)
    one=evidence()
    with pytest.raises(OwnershipError,match='ambiguous_identity_evidence'):
        identity.validate_evidence([one,evidence()])


@pytest.mark.parametrize('case,expected',[
    ('unknown','unknown_instrument'),('ambiguous','ambiguous_instrument'),
    ('no_evidence','historical_identity_unresolved'),('wrong_id','instrument_identity_mismatch'),
    ('wrong_unit','instrument_unit_mismatch'),('wrong_date','historical_identity_unresolved'),
    ('valid',None),
])
def test_snapshot_does_not_pick_first_market_or_treat_current_registry_as_history(monkeypatch,case,expected):
    entry=evidence()
    candidate=SimpleNamespace(instrument_id=UUID(entry['instrument_id']),
        market_id=UUID(entry['market_id']),market_code='TWSE',quantity_unit='shares')
    candidates=[] if case=='unknown' else [candidate,candidate] if case=='ambiguous' else [candidate]
    if case=='wrong_id':entry['instrument_id']=str(uuid4())
    if case=='wrong_unit':entry['quantity_unit']='units'
    if case=='wrong_date':entry['valid_from']=entry['valid_through']='2026-10-30'
    monkeypatch.setattr(identity,'instrument_identity_reader',lambda session:SimpleNamespace(find_by_symbol=lambda symbol:candidates))
    snapshot=identity.snapshot_identities(None,symbols=('SYNTH1',),as_of_date=date(2026,11,6),
        evidence=[] if case=='no_evidence' else [entry])
    value=snapshot['symbols']['SYNTH1']
    if expected:assert value=={'reason':expected}
    else:assert value['binding']['instrument_id']==entry['instrument_id']


def test_frozen_resolver_copies_evidence_and_requires_exact_statistical_date():
    entry=evidence()
    snapshot=dict(version='ownership-identity-snapshot-v1',as_of_date='2026-11-06',symbols={'SYNTH1':{'binding':entry}})
    resolver=FrozenIdentityResolver(snapshot)
    snapshot['symbols']['SYNTH1']['binding']['valid_through']='2020-01-01'
    binding=resolver.resolve(symbol='SYNTH1',as_of_date=date(2026,11,6))
    assert binding.valid_through==date(2026,11,6)
    with pytest.raises(OwnershipError,match='identity_snapshot_date_mismatch'):
        resolver.resolve(symbol='SYNTH1',as_of_date=date(2026,10,30))
    with pytest.raises(OwnershipError,match='unknown_instrument'):
        resolver.resolve(symbol='UNKNOWN',as_of_date=date(2026,11,6))
