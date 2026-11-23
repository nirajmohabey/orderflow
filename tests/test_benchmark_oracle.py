"""Regression for the preserved native benchmark observer timezone failure."""
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path


def test_observer_normalizes_native_postgres_timezone(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    benchmark = importlib.import_module("benchmark")
    native = datetime(2026, 11, 1, 7, tzinfo=timezone(timedelta(hours=-5)))
    assert benchmark.utc_string(native) == "2026-11-01T12:00:00Z"
    assert benchmark.utc_string(datetime(2026, 11, 1, 13, tzinfo=timezone(timedelta(hours=1)))) == "2026-11-01T12:00:00Z"
