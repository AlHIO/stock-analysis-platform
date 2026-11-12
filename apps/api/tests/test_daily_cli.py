"""SYNTHETIC daily CLI boundaries; no provider or PostgreSQL connections."""
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from stockapp import cli


def parse(*args):
    return cli._parser().parse_args(['daily', *args])


def test_daily_worker_defaults_are_taipei_2000_and_bounded():
    args = parse('worker')
    assert args.daily_action == 'worker' and args.at == '20:00'
    assert args.market_scope == 'stocks'
    assert args.max_attempts == 3
    assert args.retry_delay_seconds == 900
    assert args.job_timeout_seconds == 1800
    assert all(getattr(args, product + '_evidence') is None
               for product in ('ownership', 'margin', 'lending', 'institutional'))


def test_daily_custom_clock_and_reviewed_evidence_are_passed_as_paths():
    args = parse('worker', '--at', '22:15', '--market-scope', 'securities',
                 '--ownership-evidence', '/reviewed/ownership.json',
                 '--margin-evidence', '/reviewed/margin.json',
                 '--lending-evidence', '/reviewed/lending.json',
                 '--institutional-evidence', '/reviewed/institutional.json',
                 '--max-attempts', '2', '--retry-delay-seconds', '60',
                 '--job-timeout-seconds', '3600')
    assert args.at == '22:15' and args.market_scope == 'securities'
    assert args.max_attempts == 2 and args.retry_delay_seconds == 60
    assert args.job_timeout_seconds == 3600
    for product in ('ownership', 'margin', 'lending', 'institutional'):
        assert getattr(args, product + '_evidence') == Path(f'/reviewed/{product}.json')


@pytest.mark.parametrize('arguments', [
    (), ('unknown',), ('run', '--at', '20:00'),
    ('worker', '--at', '8:00'), ('worker', '--at', '24:00'),
    ('worker', '--at', '20:60'), ('worker', '--at', '20:00:00'),
    ('worker', '--at', '20:00Z'), ('worker', '--at', '２０:００'),
    ('worker', '--timezone', 'UTC'), ('run', '--market-scope', 'everything'),
    ('run', '--max-attempts', '0'), ('run', '--max-attempts', '4'),
    ('run', '--max-attempts', '1.5'),
    ('run', '--retry-delay-seconds', '59'), ('run', '--retry-delay-seconds', '86401'),
    ('run', '--job-timeout-seconds', '59'), ('run', '--job-timeout-seconds', '3601'),
    ('status', '--limit', '0'), ('status', '--limit', '101'),
    ('status', '--limit', 'many'), ('status', '--ownership-evidence', 'unused.json'),
    ('run', '--command', 'arbitrary-shell-command'),
])
def test_invalid_daily_arguments_never_read_settings_or_open_database(monkeypatch, arguments):
    monkeypatch.setattr(cli, 'Settings', lambda **kwargs: pytest.fail('No Settings access'))
    monkeypatch.setattr(cli, 'create_db_engine', lambda *args: pytest.fail('No database engine'))
    with pytest.raises(SystemExit) as result:
        cli.main(['daily', *arguments])
    assert result.value.code == 2


@pytest.mark.parametrize('arguments', [('--help',), ('run', '--help'),
                                      ('worker', '--help'), ('status', '--help')])
def test_daily_help_is_read_only_and_has_no_runtime_import(monkeypatch, arguments):
    monkeypatch.setattr(cli, 'Settings', lambda **kwargs: pytest.fail('Help must not read Settings'))
    monkeypatch.setattr(cli, 'create_db_engine', lambda *args: pytest.fail('Help must not create engine'))
    monkeypatch.setitem(sys.modules, 'stockapp.operations.daily.runtime', None)
    with pytest.raises(SystemExit) as result:
        cli.main(['daily', *arguments])
    assert result.value.code == 0


class SyntheticEngine:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


def configure(monkeypatch, execute):
    engine = SyntheticEngine()
    settings = SimpleNamespace(database_url='SYNTHETIC_NOT_A_REAL_DSN')
    monkeypatch.setattr(cli, 'Settings', lambda **kwargs: settings)
    monkeypatch.setattr(cli, 'create_db_engine', lambda *args: engine)
    monkeypatch.setattr(cli, 'SqlAlchemyIngestionStore', lambda *args: pytest.fail('No Phase 1 store'))
    runtime = ModuleType('stockapp.operations.daily.runtime')
    runtime.execute_daily = execute
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    return engine, settings


@pytest.mark.parametrize('status, expected', [
    ('succeeded', 0), ('unchanged', 0), ('partial', 2),
    ('failed', 2), ('blocked', 2), ('running', 2),
])
def test_daily_run_dispatch_preserves_non_success_status_and_disposes_engine(monkeypatch, capsys, status, expected):
    actual = []

    async def execute(engine, args, settings):
        actual.append((engine, args.daily_action, settings))
        return [{'status': status, 'items': [{'job_key': 'SYNTHETIC_test'}]}]

    engine, settings = configure(monkeypatch, execute)
    assert cli.main(['daily', 'run']) == expected
    assert actual == [(engine, 'run', settings)]
    assert engine.disposed
    assert json.loads(capsys.readouterr().out)[0]['status'] == status


def test_manual_run_with_no_report_is_not_reported_as_success(monkeypatch, capsys):
    async def execute(*args):
        return []

    engine, _ = configure(monkeypatch, execute)
    assert cli.main(['daily', 'run']) == 2 and engine.disposed
    assert json.loads(capsys.readouterr().out) == []


def test_status_is_successful_even_when_persisted_cycle_failed(monkeypatch, capsys):
    async def execute(engine, args, settings):
        assert args.daily_action == 'status' and args.limit == 7
        return [{'status': 'failed'}]

    engine, _ = configure(monkeypatch, execute)
    assert cli.main(['daily', 'status', '--limit', '7']) == 0 and engine.disposed
    assert json.loads(capsys.readouterr().out) == [{'status': 'failed'}]


def test_worker_normal_stop_returns_success(monkeypatch, capsys):
    async def execute(engine, args, settings):
        assert args.daily_action == 'worker' and args.at == '20:00'
        return []

    engine, _ = configure(monkeypatch, execute)
    assert cli.main(['daily', 'worker']) == 0 and engine.disposed
    assert json.loads(capsys.readouterr().out) == []


def test_daily_error_hides_exception_details_and_disposes_engine(monkeypatch, capsys):
    async def execute(*args):
        raise RuntimeError('postgresql://private:SECRET@internal/db SELECT confidential')

    engine, _ = configure(monkeypatch, execute)
    assert cli.main(['daily', 'run']) == 1 and engine.disposed
    output = capsys.readouterr().out
    assert 'SECRET' not in output and 'postgresql://' not in output and 'SELECT' not in output
    payload = json.loads(output)
    assert payload['error'] == 'RuntimeError'
    assert 'stockapp daily status' in payload['hint']


def test_daily_database_configuration_error_is_sanitized(monkeypatch, capsys):
    def fail(*args):
        raise ValueError('SYNTHETIC_SECRET_DSN')

    monkeypatch.setattr(cli, 'create_db_engine', fail)
    assert cli.main(['daily', 'status']) == 1
    output = capsys.readouterr().out
    assert 'SYNTHETIC_SECRET_DSN' not in output
    assert json.loads(output)['error'] == 'ValueError'


def test_daily_keyboard_interrupt_disposes_engine(monkeypatch):
    async def execute(*args):
        raise KeyboardInterrupt

    engine, _ = configure(monkeypatch, execute)
    assert cli.main(['daily', 'worker']) == 130 and engine.disposed
