"""Public Canonical Reader composition; no database or provider connection."""
from pathlib import Path
import subprocess
import sys

import pytest

from stockapp.modules.margin.public import MarginReadRepository, margin_reader
from stockapp.modules.lending.public import LendingReadRepository, lending_reader


@pytest.mark.parametrize(('factory', 'protocol'), [
    (margin_reader, MarginReadRepository), (lending_reader, LendingReadRepository),
])
def test_public_reader_keeps_callers_session_without_io(factory, protocol):
    class NoIoSession:
        def __getattr__(self, operation):
            raise AssertionError(f'Reader assembly unexpectedly accessed Session.{operation}')

    session = NoIoSession()
    reader = factory(session)
    assert reader.session is session
    for name, member in vars(protocol).items():
        if not name.startswith('_') and callable(member):
            assert callable(getattr(reader, name)), name


def test_ledger_reader_public_import_remains_standard_library_only():
    source = Path(__file__).resolve().parents[1] / 'src'
    script = (
        f'import sys; sys.path.insert(0, {str(source)!r}); '
        'from stockapp.modules.margin.public import margin_reader, MarginReadRepository, MarginQueryError; '
        'from stockapp.modules.lending.public import lending_reader, LendingReadRepository, LendingQueryError; '
        "assert not any(name in sys.modules for name in ('sqlalchemy', 'fastapi', 'redis', 'httpx'))"
    )
    result = subprocess.run([sys.executable, '-S', '-c', script],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
