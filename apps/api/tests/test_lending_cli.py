"""SYNTHETIC CLI checks; never open a live source or database."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp import cli
from stockapp.modules.lending.domain.models import LendingError
from stockapp.modules.lending.infrastructure import operations
from stockapp.modules.lending.infrastructure.source_manifest import MANIFESTS


def parse(*args):return cli._parser().parse_args(['lending',*args])


def test_latest_defaults_to_only_approved_m6_source():
    args=parse('sync','latest')
    assert args.source=='M6' and args.date is None and args.identity_evidence is None
    spec=operations.source_spec(MANIFESTS['M6'])
    assert spec.source_code=='tpex_lending_short_openapi'
    assert spec.config['contract']['product']=='lending_short_sales'
    assert spec.config['contract']['channel']['channel']=='unspecified'
    assert spec.config['contract']['channel']['coverage']==[]
    assert spec.config['default_network_enabled'] is False


@pytest.mark.parametrize('arguments',[
    ('sync',),('sync','date'),('sync','date','--date','2026-02-30'),
    ('sync','latest','--source','M0'),('sync','latest','--date','2026-11-06'),
    ('replay',),('replay','--raw','invalid'),('status','--limit','0'),
    ('status','--limit','101'),('status','--id','invalid'),
])
def test_invalid_commands_have_no_config_or_database_io(monkeypatch,arguments):
    monkeypatch.setattr(cli,'Settings',lambda **kw:pytest.fail('no Settings'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *a:pytest.fail('no Engine'))
    with pytest.raises(SystemExit) as error:cli.main(['lending',*arguments])
    assert error.value.code==2


@pytest.mark.parametrize('arguments',[
    ('--help',),('sync','--help'),('sync','latest','--help'),
    ('sync','date','--help'),('replay','--help'),('status','--help'),
])
def test_help_needs_no_runtime_dependencies(monkeypatch,arguments):
    monkeypatch.setattr(cli,'Settings',lambda **kw:pytest.fail('no Settings'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *a:pytest.fail('no Engine'))
    with pytest.raises(SystemExit) as error:cli.main(['lending',*arguments])
    assert error.value.code==0


def test_resume_cannot_load_changed_evidence(monkeypatch):
    monkeypatch.setattr(cli,'Settings',lambda **kw:pytest.fail('no Settings'))
    with pytest.raises(SystemExit) as error:
        cli.main(['lending','replay','--resume',str(uuid4()),'--evidence','changed.json'])
    assert error.value.code==2


@pytest.mark.parametrize('status,code',[('succeeded',0),('partial',2),('failed',2),('running',2)])
def test_sync_exit_code_does_not_hide_partial_or_failure(status,code):
    assert operations.exit_code(parse('sync','latest'),[{'status':status}])==code


def test_status_exit_code_describes_lookup():
    assert operations.exit_code(parse('status'),[{'status':'failed'}])==0
    assert operations.exit_code(parse('status'),[])==0
    assert operations.exit_code(parse('status','--id',str(uuid4())),[])==2


class Engine:
    disposed=False
    def dispose(self):self.disposed=True


def configure(monkeypatch):
    engine=Engine()
    monkeypatch.setattr(cli,'Settings',lambda **kw:SimpleNamespace(database_url='SYNTHETIC'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *a:engine)
    monkeypatch.setattr(cli,'SqlAlchemyIngestionStore',lambda *a:pytest.fail('no Phase1 writer'))
    return engine


def test_dispatch_uses_lending_only_and_disposes_engine(monkeypatch,capsys):
    engine=configure(monkeypatch)
    async def execute(actual,args,settings):
        assert actual is engine and args.lending_action=='sync'
        return [{'status':'partial','stats':{'accepted':1,'unresolved':2}}]
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['lending','sync','latest'])==2 and engine.disposed
    assert json.loads(capsys.readouterr().out)[0]['stats']['accepted']==1


@pytest.mark.parametrize('error,code',[
    (RuntimeError('postgresql://private:SECRET@host/db'),1),
    (LendingError('source_authorization_pending'),2),
])
def test_errors_never_print_internal_connections(monkeypatch,capsys,error,code):
    engine=configure(monkeypatch)
    async def execute(*args):raise error
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['lending','status'])==code and engine.disposed
    output=capsys.readouterr().out
    assert 'SECRET' not in output and 'postgresql://' not in output


def test_status_never_constructs_provider(monkeypatch):
    class Store:
        def inspect(self,identifier,*,limit):
            assert identifier is None and limit==7
            return []
    monkeypatch.setattr(operations,'SqlAlchemyLendingIngestionStore',lambda *a,**kw:Store())
    monkeypatch.setattr(operations,'RuntimeProvider',lambda *a:pytest.fail('no Provider'))
    assert asyncio.run(operations.execute(object(),parse('status','--limit','7'),
        SimpleNamespace(ingestion_min_interval_seconds=5)))==[]


def test_replay_resume_resolves_manifest_by_contract_key_not_audit_id():
    args=parse('replay','--resume',str(uuid4()))
    manifest=MANIFESTS['M6']
    class Store:
        def get(self,run_id):return SimpleNamespace(config={'contract':{'key':manifest.contract.key}})
    assert operations._manifest(args,Store()) is manifest
