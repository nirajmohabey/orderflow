"""Prevent ambiguous backup destinations and evidence output escapes before I/O."""
import pytest
from scripts import verification_support as verification


@pytest.mark.parametrize('key',['dbname','hostaddr','service','options'])
def test_invalid_runtime_destination_is_rejected_before_admin_connection(monkeypatch,key):
    monkeypatch.setattr(verification,'create_engine',lambda *_a,**_k:pytest.fail('Connection opened before both URLs were validated'))
    admin='postgresql+psycopg://fixture:fixture@127.0.0.1:55449/orderflow_restore_012345abcdef'
    with pytest.raises(RuntimeError,match='routing overrides'):
        verification.validate_destination_pair(admin,admin+'?'+key+'=other','orderflow_restore_012345abcdef')


@pytest.mark.parametrize('destination',['sqlite:///orderflow','postgresql+psycopg:///orderflow','postgresql+psycopg://127.0.0.1'])
def test_backup_requires_explicit_postgresql_server_and_database(destination):
    with pytest.raises(RuntimeError,match='explicit PostgreSQL'):
        verification.validate_connection_url(destination)


def test_verification_output_cannot_overwrite_files_outside_artifacts(monkeypatch,tmp_path):
    sentinel=tmp_path/'preserved.txt'
    sentinel.write_text('preserve')
    monkeypatch.setenv('ODF_VERIFICATION_OUTPUT',str(sentinel))
    with pytest.raises(RuntimeError,match='inside project artifacts'):
        verification.report_output('artifacts/checks/backup-restore.json')
    assert sentinel.read_text()=='preserve'


def test_backup_partial_dump_is_removed_even_when_engine_disposal_fails(monkeypatch, tmp_path):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from scripts import backup_check
    from orderflow import db

    values = {
        'ODF_ADMIN_DATABASE_URL': 'postgresql+psycopg://fixture:fixture@127.0.0.1:55449/orderflow',
        'ODF_DATABASE_URL': 'postgresql+psycopg://fixture:fixture@127.0.0.1:55449/orderflow',
    }
    connections = []

    class Result:
        def __init__(self, value): self.value = value
        def fetchone(self): return self.value

    class Source:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement, *args):
            value = ('orderflow', '127.0.0.1', 55449) if 'current_database' in str(statement) else ('fixture-snapshot',)
            return Result(value)

    def connect(**kwargs):
        connections.append(kwargs['dbname'])
        return Source()

    def partial_dump_then_fail(command, **kwargs):
        Path(command[command.index('--file') + 1]).write_bytes(b'synthetic partial dump without private data')
        raise RuntimeError('Synthetic pg_dump failure')

    def disposal_fails():
        raise RuntimeError('Synthetic engine disposal failure')

    report_path = tmp_path / 'report.json'
    monkeypatch.setattr(backup_check, 'ROOT', tmp_path)
    monkeypatch.setattr(backup_check, 'dotenv_values', lambda _: values)
    monkeypatch.setattr(backup_check, 'report_output', lambda _: report_path)
    monkeypatch.setattr(backup_check, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    monkeypatch.setattr(backup_check.psycopg, 'connect', connect)
    monkeypatch.setattr(backup_check, 'digest_rows', lambda _: {})
    monkeypatch.setattr(backup_check, 'binary', lambda _: 'synthetic-pg-dump')
    monkeypatch.setattr(backup_check.subprocess, 'run', partial_dump_then_fail)
    monkeypatch.setattr(db, 'dispose_engines', disposal_fails)

    with pytest.raises(RuntimeError):
        backup_check.main()
    assert connections == ['orderflow']
    assert not list((tmp_path / '.runtime/backups').glob('*.dump'))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['private_dump_removed'] is True
    assert report['passed'] is False
