"""SYNTHETIC CLI dispatch and failures; no database or network connections."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp import cli
from stockapp.modules.margin.domain.models import MarginError
from stockapp.modules.margin.infrastructure import operations
from stockapp.modules.margin.infrastructure.source_manifest import TPEX_MANIFEST


def parse(*args):
    return cli._parser().parse_args(['margin',*args])


def test_latest_source_defaults_and_explicit_reviewed_evidence():
    args=parse('sync','latest')
    assert args.market=='tpex' and args.source=='openapi' and args.date is None
    assert args.identity_evidence is None
    spec=operations.source_spec(TPEX_MANIFEST)
    assert spec.source_code=='tpex_margin_openapi'
    assert spec.config['authorization']=='ogdl_dataset_documented'
    assert spec.config['contract']['capability']=='latest_only'
    assert spec.config['default_network_enabled'] is False


@pytest.mark.parametrize('arguments', [
    ('sync',),('sync','date'),('sync','date','--date','2026-02-30'),
    ('sync','latest','--market','invalid'),('sync','latest','--date','2026-11-06'),
    ('replay',),('replay','--raw','invalid'),('status','--limit','0'),
    ('status','--limit','101'),('status','--id','not-a-uuid'),
])
def test_invalid_command_never_reads_settings_or_opens_engine(monkeypatch,arguments):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('no Settings'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:pytest.fail('no Engine'))
    with pytest.raises(SystemExit) as error:
        cli.main(['margin',*arguments])
    assert error.value.code==2


@pytest.mark.parametrize('arguments', [
    ('--help',),('sync','--help'),('sync','latest','--help'),
    ('sync','date','--help'),('replay','--help'),('status','--help'),
])
def test_help_is_available_without_runtime_dependencies(monkeypatch,arguments):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('no Settings'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:pytest.fail('no Engine'))
    with pytest.raises(SystemExit) as error:
        cli.main(['margin',*arguments])
    assert error.value.code==0


def test_resume_uses_frozen_evidence_and_rejects_changed_file(monkeypatch):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:pytest.fail('no Settings'))
    with pytest.raises(SystemExit) as error:
        cli.main(['margin','replay','--resume',str(uuid4()),'--evidence','changed.json'])
    assert error.value.code==2


@pytest.mark.parametrize('status,code', [('succeeded',0),('partial',2),('failed',2),('running',2)])
def test_sync_exit_code_preserves_non_success_status(status,code):
    assert operations.exit_code(parse('sync','latest'),[{'status':status}])==code


def test_status_lookup_does_not_treat_failed_run_as_cli_error():
    assert operations.exit_code(parse('status'),[{'status':'failed'}])==0
    assert operations.exit_code(parse('status'),[])==0
    assert operations.exit_code(parse('status','--id',str(uuid4())),[])==2


class Engine:
    disposed=False
    def dispose(self):self.disposed=True


def configure(monkeypatch):
    engine=Engine()
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:SimpleNamespace(database_url='SYNTHETIC'))
    monkeypatch.setattr(cli,'create_db_engine',lambda *args:engine)
    monkeypatch.setattr(cli,'SqlAlchemyIngestionStore',lambda *args:pytest.fail('no Phase1 store'))
    return engine


def test_dispatch_owns_and_disposes_only_its_engine(monkeypatch,capsys):
    engine=configure(monkeypatch)
    async def execute(actual,args,settings):
        assert actual is engine and args.margin_action=='sync'
        return [{'status':'partial','stats':{'accepted':1,'unresolved':2}}]
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['margin','sync','latest'])==2 and engine.disposed
    assert json.loads(capsys.readouterr().out)[0]['stats']['accepted']==1


@pytest.mark.parametrize('error,code', [
    (RuntimeError('postgresql://private:SECRET@host/db'),1),
    (MarginError('source_authorization_pending'),2),
])
def test_errors_are_sanitized_and_engine_disposed(monkeypatch,capsys,error,code):
    engine=configure(monkeypatch)
    async def execute(*args):raise error
    monkeypatch.setattr(operations,'execute',execute)
    assert cli.main(['margin','status'])==code and engine.disposed
    output=capsys.readouterr().out
    assert 'SECRET' not in output and 'postgresql://' not in output


def test_status_does_not_construct_provider(monkeypatch):
    class Store:
        def inspect(self,identifier,*,limit):
            assert identifier is None and limit==7
            return []
    monkeypatch.setattr(operations,'SqlAlchemyMarginIngestionStore',lambda *a,**kw:Store())
    monkeypatch.setattr(operations,'RuntimeProvider',lambda *a:pytest.fail('no provider'))
    result=asyncio.run(operations.execute(object(),parse('status','--limit','7'),
        SimpleNamespace(ingestion_min_interval_seconds=5)))
    assert result==[]
