"""Regressions for connection-query routing overrides in destructive test setup."""
import pytest
from scripts import verification_support as verification

@pytest.mark.parametrize('key',['dbname','database','host','hostaddr','port','user','password','service','servicefile','options'])
def test_routing_override_rejected_before_any_connection(monkeypatch,key):
    def unexpected_connection(*args,**kwargs):
        raise AssertionError('Routing override reached a database connection')
    monkeypatch.setattr(verification,'create_engine',unexpected_connection)
    url='postgresql+psycopg://fixture:fixture@127.0.0.1:55449/orderflow_oracle_012345abcdef?'+key+'=other'
    with pytest.raises(RuntimeError,match='routing overrides'):
        verification.validate_destination_pair(url,url,'orderflow_oracle_012345abcdef')

def test_unowned_database_name_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(verification,'create_engine',lambda *_a,**_k:pytest.fail('Unexpected connection'))
    with pytest.raises(RuntimeError,match='Unrecognized owned'):
        verification.validate_destination_pair('unused','unused','orderflow')
