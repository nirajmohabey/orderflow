"""Append classified partial evidence without changing strict release gates.

This adapter is outside the selected 104-test input scope. It does not repair,
rerun, promote, or rewrite any benchmark report. Review before execution.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_manifest import Evidence, PAIRS, benchmark_harness, bind_raw, build_manifest, require, verify_manifest
from package_release import member_allowed
from run_tests import core_snapshot, safe_artifact, sha, snapshot


def checked_item(evidence, item):
    require(isinstance(item, dict), "Indexed file entry is not an object")
    name = item.get("path")
    require(isinstance(name, str) and name and "\\" not in name, "Indexed path must be portable POSIX text")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, "Indexed path must stay project-relative")
    require(path.parts[0] in {"artifacts", "analysis"}, "Supplementary entry is outside analysis/artifacts")
    member_allowed("orderflow/" + name)
    require(isinstance(item.get("sha256"), str) and re.fullmatch("[0-9a-f]{64}", item["sha256"]), "Missing/invalid indexed SHA-256")
    require(type(item.get("bytes")) is int and item["bytes"] >= 0, "Missing/invalid indexed byte count")
    known = evidence.files.get(name)
    if known:
        require(known["sha256"] == item["sha256"] and known["bytes"] == item["bytes"], "Conflicting duplicate file binding")
    return evidence.bind(name, item)


def checked_raw(evidence, items):
    require(isinstance(items, list) and items, "Report has no raw/input index")
    for item in items:
        require(isinstance(item, dict) and type(item.get("bytes")) is int and item["bytes"] >= 0, "Raw artifact byte count is missing/invalid")
        require(isinstance(item.get("sha256"), str) and re.fullmatch("[0-9a-f]{64}", item["sha256"]), "Raw artifact SHA-256 is missing/invalid")
        known = evidence.files.get(item.get("path"))
        if known:
            require(known["sha256"] == item.get("sha256") and known["bytes"] == item.get("bytes"), "Conflicting duplicate raw binding")
        else:
            # Includes full gzip CRC/decompression and raw-directory boundaries.
            bind_raw(evidence, [item])


def report_group(evidence, entries, scale, expected_passes):
    require(isinstance(entries, list) and len(entries) == 6, "Each workload must retain all six reports")
    current_core, current_harness = core_snapshot(), benchmark_harness()
    identities, records = set(), []
    for item in entries:
        bound = checked_item(evidence, item)
        value = json.loads((ROOT / bound["path"]).read_text(encoding="utf-8"))
        identity = (value.get("workers"), value.get("repetition"))
        require(identity in PAIRS and identity not in identities, "Duplicate/unexpected worker/repetition")
        identities.add(identity)
        require(type(value.get("passed")) is bool, "Original report has no actual Boolean outcome")
        trial = value.get("trial")
        require(isinstance(trial, dict) and trial.get("passed") is value["passed"], "Original outer/trial outcomes differ")
        require((trial.get("workers"), trial.get("repetition")) == identity, "Nested trial identity differs")
        require(value.get("source") == trial.get("source") == current_core, "Original report core/SQL identity differs")
        require(value.get("harness_sha256") == trial.get("harness_sha256") == current_harness, "Original selected harness identity differs")
        require(trial.get("distinct_accepted_ids") == scale and trial.get("http_attempts") == scale + scale // 10, "Original completed workload scale differs")
        checked_raw(evidence, value.get("inputs"))
        checked_raw(evidence, trial.get("raw_artifacts"))
        records.append({**bound, "workers": identity[0], "repetition": identity[1], "passed": value["passed"], "classification": "supplementary_partial_not_verified"})
    require(identities == PAIRS, "Workload is missing a scheduled report identity")
    require(sum(record["passed"] for record in records) == expected_passes, "Actual report outcome count differs from partial audit")
    return records


def main(argv=None):
    if not __debug__ or sys.flags.optimize:
        raise RuntimeError("Partial binding requires non-optimized Python")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", default="artifacts/analysis/summary.json")
    parser.add_argument("--os-report", default="artifacts/security/container-os-audit/os-audit.json")
    parser.add_argument("--ci-report", default="artifacts/ci/remote-run.json")
    parser.add_argument("--output", default="artifacts/release_manifest.json")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    before = snapshot()
    adapter = Path(__file__).resolve()
    adapter_sha = sha(adapter)
    base_args = argparse.Namespace(native_xml="artifacts/checks/full-suite.xml", linux_xml="artifacts/checks/linux-full-suite.xml", small_benchmark=None, large_benchmark=None, os_report=args.os_report, ci_report=args.ci_report, output=args.output, check_only=args.check_only)
    base = build_manifest(base_args)
    require(base.get("passed") is True, "Strict base release gates did not pass")
    require(base.get("benchmarks", {}).get("status") == "not_verified", "Base benchmark status must remain not_verified")
    require(base["gates"]["benchmark_selection"]["detail"]["status"] == "not_verified", "Strict benchmark gate was promoted")
    original_gates, original_benchmarks = copy.deepcopy(base["gates"]), copy.deepcopy(base["benchmarks"])
    extra = Evidence()
    audit_path = safe_artifact(args.audit)
    audit_binding = extra.bind(audit_path.relative_to(ROOT).as_posix())
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    require(audit.get("status") == "partial" and audit.get("passed") is False and audit.get("benchmark_gate_status") == "not_verified", "Audit does not preserve partial/not_verified outcome")
    require(audit.get("source_sha256") == core_snapshot()["sha256"], "Partial audit describes another core/SQL tree")
    require((audit.get("small_passed"), audit.get("large_passed"), audit.get("large_failed")) == (6, 5, 1), "Audit counts differ from expected actual partial outcomes")
    small = report_group(extra, audit.get("small_reports"), 10000, 6)
    large = report_group(extra, audit.get("large_reports"), 100000, 5)
    failed = [record for record in large if not record["passed"]]
    require([(record["workers"], record["repetition"]) for record in failed] == [(1, 1)], "The original failed 1-worker/repetition-1 report must remain failed")
    files = audit.get("files")
    require(isinstance(files, list) and files, "Audit files index is absent")
    require(len({item.get("path") for item in files}) == len(files), "Audit contains duplicate file-index paths")
    for item in files:
        checked_item(extra, item)
    require(any(item["path"].endswith(".html") for item in files) and any(item["path"].endswith(".png") for item in files), "Rendered HTML/PNG evidence is absent")
    adapter_binding = extra.bind(adapter.relative_to(ROOT).as_posix())
    merged = {item["path"]: item for item in base["evidence_files"]}
    for name, item in extra.files.items():
        if name in merged:
            require(merged[name] == item, "Base and supplementary bindings conflict")
        merged[name] = item
    base["evidence_files"] = list(merged.values())
    base["supplementary_partial_evidence"] = {
        "status": "partial", "passed": False, "benchmark_gate_status": "not_verified",
        "bound_at": datetime.now(timezone.utc).isoformat(), "audit": audit_binding, "adapter": adapter_binding,
        "classification": "supplementary_partial_not_verified", "small_reports": small, "large_reports": large,
        "small_passed": 6, "large_passed": 5, "large_failed": 1,
        "files": list(extra.files.values()), "gates_unchanged": True,
        "scope": "Append-only byte binding for original partial observations, including the immutable failed trial, raw gzip, inputs, profiles, protocol evidence and rendered analysis. No benchmark outcome, gate, measured value, or release passed flag is promoted or changed.",
    }
    require(base["gates"] == original_gates and base["benchmarks"] == original_benchmarks and base["passed"] is True, "Strict base gate/status mutation is forbidden")
    require(snapshot() == before and sha(adapter) == adapter_sha and sha(audit_path) == audit_binding["sha256"], "Selected source, adapter, or audit changed during binding")
    verify_manifest(base)
    if not args.check_only:
        target = safe_artifact(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(base, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"strict_base_passed": base["passed"], "benchmark_status": base["benchmarks"]["status"], "supplementary_status": "partial", "supplementary_passed": False, "small_passed": 6, "large_passed": 5, "large_failed": 1, "bound_files": len(extra.files), "gates_unchanged": True, "adapter_sha256": adapter_sha, "manifest_written": not args.check_only}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
