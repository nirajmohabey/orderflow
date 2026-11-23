"""The large-trial dispatcher must fail closed before opening a database."""
import importlib
import json
from pathlib import Path

import pytest


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    return importlib.import_module('benchmark_trial')


def test_large_trial_rejects_gate_bytes_changed_after_review(monkeypatch,tmp_path):
    runner=module(monkeypatch)
    gate=tmp_path/'gate.json';gate.write_text('{"complete":false}')
    with pytest.raises(RuntimeError,match='reviewed SHA256'):
        runner.validate_gate(gate,'0'*64,20261123)


def test_large_trial_rejects_incomplete_gate_before_source_or_database_work(monkeypatch,tmp_path):
    runner=module(monkeypatch)
    gate=tmp_path/'gate.json';gate.write_text(json.dumps({'schema':'orderflow-core-benchmark-v1','complete':False,'passed':False}))
    monkeypatch.setattr(runner.benchmark,'source_manifest',lambda: (_ for _ in ()).throw(AssertionError('No source/DB work should begin for an incomplete gate')))
    with pytest.raises(RuntimeError,match='not complete and passing'):
        runner.validate_gate(gate,runner.benchmark.sha(gate),20261123)


def test_runtime_pin_names_snapshot_honestly_and_rejects_mutable_tags(monkeypatch):
    runner=module(monkeypatch)
    assert runner.runtime_pin_kind('snapshot:snap_testfixture123')=='provider_immutable_snapshot'
    assert runner.runtime_pin_kind('vercel/sandbox/universal@sha256:'+'a'*64)=='image_sha256_digest'
    with pytest.raises(ValueError):runner.runtime_pin_kind('vercel/sandbox/universal:latest')
