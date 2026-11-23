"""CPU/mocked future-harness guards; no workload, PostgreSQL or cloud execution."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def benchmark():
    path = Path(__file__).resolve().parents[1] / "analysis/benchmark_v2.py"
    spec = importlib.util.spec_from_file_location("orderflow_benchmark_v2_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    source = {"files": {"mock-source": "1" * 64}, "sha256": "2" * 64}
    harness = {"analysis/benchmark_v2.py": "3" * 64}
    raw = tmp_path / "artifacts/benchmarks/raw/future"
    raw.mkdir(parents=True)
    def artifact(path):
        benchmark.packed_json(path, [{"mocked": True}])
        return {"path": path.relative_to(tmp_path).as_posix(), "bytes": path.stat().st_size, "sha256": benchmark.sha(path)}
    value = {"schema": "orderflow-core-benchmark-v1", "complete": True, "passed": True,
             "source": source, "harness_sha256": harness, "seed": 20261123,
             "mix": {"distinct_accepted": 10000},
             "inputs": [artifact(raw / "input.jsonl.gz"), artifact(raw / "truth-ledger.jsonl.gz")], "runs": []}
    for workers in (1, 4):
        for repetition in (1, 2, 3):
            folder = raw / f"workers-{workers}-rep-{repetition}"
            folder.mkdir()
            value["runs"].append({"workers": workers, "repetition": repetition, "source": source,
                "harness_sha256": harness, "passed": True, "owned_database_removed": True, "http_errors": [],
                "distinct_accepted_ids": 10000, "http_attempts": 11000, "request_concurrency": 24,
                "reconciliation": {"passed": True, "checks": [{"name": f"check-{n}", "passed": True} for n in range(19)]},
                "postflight_validation": {"protocol_id": "postflight-token-refresh-v1", "measurement_finished_before_refresh": True,
                    "refresh_statuses": {"alpha-operator": 200, "beta-operator": 200},
                    "overview_statuses": {"alpha-operator": 200, "beta-operator": 200}, "cross_tenant_status": 404},
                "raw_artifacts": [artifact(folder / "http-attempts.jsonl.gz"), artifact(folder / "terminal-visibility.jsonl.gz")]})
    path = tmp_path / "artifacts/benchmarks/future-10000-summary.json"
    def validate():
        path.write_text(json.dumps(value), encoding="utf-8")
        return benchmark.verify_small_gate(path, source, harness, 20261123, 24)
    return SimpleNamespace(value=value, path=path, validate=validate, root=tmp_path)


def test_v2_small_gate_accepts_six_complete_current_bound_trials(gate):
    result = gate.validate()
    assert result["path"] == "artifacts/benchmarks/future-10000-summary.json"
    assert len(result["sha256"]) == 64


@pytest.mark.parametrize("problem", ["empty", "duplicate", "failed", "cleanup", "source", "reconciliation", "postflight"])
def test_v2_small_gate_rejects_flag_only_or_incomplete_evidence(gate, problem):
    first = gate.value["runs"][0]
    if problem == "empty": gate.value["runs"] = []
    elif problem == "duplicate": gate.value["runs"][-1] = first.copy()
    elif problem == "failed": first["passed"] = False
    elif problem == "cleanup": first["owned_database_removed"] = False
    elif problem == "source": first["source"] = {"sha256": "stale"}
    elif problem == "reconciliation": first["reconciliation"]["checks"].pop()
    elif problem == "postflight": first["postflight_validation"]["overview_statuses"]["alpha-operator"] = 401
    with pytest.raises(RuntimeError): gate.validate()


def test_v2_small_gate_rejects_changed_raw_file(gate):
    item = gate.value["runs"][0]["raw_artifacts"][0]
    (gate.root / item["path"]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="bytes or SHA-256 changed"): gate.validate()


def test_v2_small_gate_checks_gzip_crc_even_with_current_hash(gate, benchmark):
    item = gate.value["runs"][0]["raw_artifacts"][0]
    path = gate.root / item["path"]
    path.write_bytes(path.read_bytes()[:-4])
    item.update(bytes=path.stat().st_size, sha256=benchmark.sha(path))
    with pytest.raises(RuntimeError, match="gzip/CRC"): gate.validate()


def test_v2_small_gate_rejects_raw_path_outside_bounded_directory(gate):
    gate.value["runs"][0]["raw_artifacts"][0]["path"] = "../private-file"
    with pytest.raises(RuntimeError, match="project-relative"): gate.validate()


def test_v2_small_gate_rejects_reused_trial_raw_files(gate):
    gate.value["runs"][1]["raw_artifacts"] = gate.value["runs"][0]["raw_artifacts"]
    with pytest.raises(RuntimeError, match="reuse"): gate.validate()


def test_v2_observer_recovers_old_transaction_marker_before_drain(benchmark, monkeypatch, tmp_path):
    base = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    recent = (benchmark.TENANTS[0], benchmark.SOURCES[0], "recent", "event-recent", base,
              "applied_current", base + timedelta(seconds=60), base + timedelta(seconds=65))
    late = (benchmark.TENANTS[1], benchmark.SOURCES[2], "late", "event-late", base,
            "applied_current", base, base + timedelta(seconds=65))
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
        def __iter__(self): return iter(self.rows)
    class Connection:
        def execute(self, query, params=None):
            if params is None: return Result([recent, late])
            tenant, cutoff = params
            return Result([row for row in (recent, late) if row[0] == tenant and row[6] >= cutoff])
    @contextmanager
    def connect(**kwargs): yield Connection()
    clock = iter(n * .25 for n in range(100))
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    monkeypatch.setattr(benchmark.psycopg, "connect", connect)
    observer = benchmark.VisibilityObserver({"kwargs": {}, "name": "mock-only"}, tmp_path / "visible.jsonl.gz", 2)
    class Stop:
        stopped = False
        cycles = 0
        def is_set(self): return self.stopped
        def wait(self, seconds):
            self.cycles += 1
            self.stopped = observer.drained.is_set() or self.cycles >= 5
    observer.stop = Stop()
    observer.run()
    assert observer.error is None
    assert observer.drained.is_set()
    assert observer.idle_catchup_scans == 1
    record = observer.seen[(late[0], late[1], late[2])]
    assert record["observation_scan"] == "idle_catchup"
    assert not record.get("final_scan_only")
    assert observer.last_observed_monotonic is not None
