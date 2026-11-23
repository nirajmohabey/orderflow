"""CI helpers cannot act on a normal user's environment by default."""
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize('name',['ci_setup','ci_airflow','ci_live','ci_docker_smoke'])
def test_ci_entrypoints_require_explicit_owned_job(monkeypatch,name):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    monkeypatch.delenv('CI',raising=False)
    module=importlib.import_module(name)
    with pytest.raises(RuntimeError,match='CI|isolated'):
        module.main()
