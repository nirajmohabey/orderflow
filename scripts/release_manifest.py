"""Bind v0.1.0 claims to current bytes and executed evidence, never prototypes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import statistics
import sys

from run_tests import ROOT, core_snapshot, digest, safe_artifact, sha, snapshot, xml_counts

PAIRS = {(workers, repetition) for workers in (1, 4) for repetition in (1, 2, 3)}


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def repo_path(name):
    path = Path(name)
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    require(path.is_relative_to(ROOT), "Evidence path escapes project: " + str(name))
    require(path.is_file(), "Missing evidence: " + str(name))
    return path


def current_map(files):
    require(isinstance(files, dict) and files, "Empty file-hash binding")
    for name, expected in files.items():
        require(sha(repo_path(name)) == expected, "Changed bound input: " + name)


class Evidence:
    def __init__(self):
        self.files = {}
        self.gates = {}

    def bind(self, name, expected=None):
        path = repo_path(name)
        relative = path.relative_to(ROOT).as_posix()
        result = {"path": relative, "sha256": sha(path), "bytes": path.stat().st_size}
        if expected:
            require(result["sha256"] == expected.get("sha256"), "Evidence hash differs: " + relative)
            if "bytes" in expected:
                require(result["bytes"] == expected["bytes"], "Evidence size differs: " + relative)
        self.files[relative] = result
        return result

    def read(self, name):
        item = self.bind(name)
        return json.loads((ROOT / item["path"]).read_text(encoding="utf-8-sig"))

    def gate(self, name, function):
        try:
            detail = function()
            result = {"passed": True, "detail": detail}
        except (OSError, ValueError, KeyError, TypeError, AssertionError, SyntaxError) as error:
            result = {"passed": False, "error": str(error)}
        self.gates[name] = result
        return result.get("detail")


def verify_proof(evidence, result_path, kind="pytest", expected_platform=None):
    result = evidence.bind(result_path)
    proof = evidence.read(str(Path(result_path).with_suffix(".source-proof.json")))
    require(proof.get("schema") == "orderflow-test-source-proof-v1", "Wrong test-source proof schema")
    require(proof.get("kind") == kind and proof.get("passed") is True, "Run was not successful: " + result_path)
    require(proof.get("assertions_enabled") is True and proof.get("exit_code") == 0, "Assertions/exit status do not pass")
    require(proof.get("result", {}).get("fresh") is True and proof["result"]["sha256"] == result["sha256"], "Result/proof bytes or freshness differ")
    require(proof.get("source_changed") is False and proof.get("source_before") == proof.get("source_after"), "Inputs changed during run")
    require(proof.get("source_after") == snapshot(), "Test inputs differ from current release bytes")
    require(proof.get("core_before") == proof.get("core_after") == core_snapshot(), "Core/SQL changed since run")
    if expected_platform:
        require(proof.get("platform", "").lower().startswith(expected_platform), "Wrong platform proof: " + expected_platform)
    if kind == "pytest":
        counts = xml_counts(ROOT / result["path"])
        require(proof.get("complete_default_suite") is True, "Selected/partial pytest run is not a full-suite release proof")
        require(counts == proof.get("counts"), "JUnit/proof counts differ")
        require(counts["tests"] > 0 and all(counts[key] == 0 for key in ("failures", "errors", "skipped")), "Full suite has failures, errors, skips or no tests")
        return {"counts": counts, "platform": proof["platform"], "python": proof["python"], "result": result}
    return proof


def verify_harness(evidence, name, path, expected_checks=None):
    verify_proof(evidence, path, name)
    value = evidence.read(path)
    require(value.get("passed") is True, "Harness did not pass: " + name)
    if expected_checks:
        checks = value.get("checks", [])
        require(len(checks) == expected_checks and all(check.get("passed") is True for check in checks), "Incomplete harness checks: " + name)
    if name in {"acceptance", "recovery"}:
        require(value.get("owned_database_removed") is True, "Owned harness database not removed")
    if name == "acceptance":
        require(value.get("configured_database_reset") is False, "Configured data was reset")
    if name == "backup":
        require(value.get("source_database_written") is False and value.get("owned_restore_removed") is True, "Restore isolation/preservation failed")
        require(value.get("table_count") == len(value.get("table_hashes", {})) == 23, "Expected 23-table restore verification is absent")
        require(value.get("duplicate_replays") == 14 and value.get("new_worker_effects") == 0, "Restore replay effect oracle failed")
    return value


def verify_browser(evidence, path, expected, scans):
    value = evidence.read(path)
    require(value.get("passed") == value.get("total") == expected and value.get("source_stable") is True, "Browser total/source stability failed")
    rows = value.get("results", [])
    require(len(rows) == expected and all(row.get("passed") is True for row in rows), "Browser scenarios are incomplete")
    require(isinstance(value.get("source_hashes"), dict) and value["source_hashes"], "Browser source binding is empty")
    browser_files = {"dashboard/" + name: expected for name, expected in value["source_hashes"].items()}
    current_map(browser_files)
    for name, expected_digest in browser_files.items():
        evidence.bind(name, {"sha256": expected_digest})
    require(not value.get("pageerrors") and not value.get("console_errors"), "Actual browser errors were recorded")
    require(not any(row.get("pageerrors") for row in rows), "Isolated browser page errors were recorded")
    accessibility = value.get("accessibility", [])
    require(len(accessibility) == scans and not any(state.get("violations") for state in accessibility), "Accessibility states/violations differ")
    return {"passed": expected, "total": expected, "accessibility_states": len(accessibility), "automated_violations": 0, "source_binding": "frontend file hashes; backend behavior is separately bound by full-suite/harness proofs"}


def verify_security(evidence):
    audit = evidence.read("artifacts/security/audit-summary.json")
    require(audit.get("required_graph_passed") is True and audit.get("required_graph_packages_with_advisories") == 0, "Selected package graphs have advisories or are missing")
    require(audit.get("missing_required_graphs") == [] and audit.get("npm", {}).get("reported_vulnerabilities") == 0, "Required graph/npm verification is incomplete")
    for name, info in audit.get("inputs", {}).items():
        evidence.bind(name, info)
    require(bool(audit.get("inputs")), "Advisory report is not bound to dependency inputs")
    raw_npm = evidence.read("artifacts/security/npm-audit.json")
    require(raw_npm.get("metadata", {}).get("vulnerabilities", {}).get("total") == 0, "Raw npm report differs")
    evidence.bind("artifacts/security/osv-audit.json")
    exposure = evidence.read("artifacts/security/source-exposure.json")
    require(exposure.get("findings") == [] and exposure.get("text_files_scanned", 0) > 0, "Source exposure scan is failed or absent")
    static = evidence.read("artifacts/security/static-audit-summary.json")
    require(static.get("passed") is True and static.get("ruff_undefined_names") == 0 and static.get("unreviewed_medium_findings") == [], "Static review is failed or incomplete")
    require(static.get("bandit_severities", {}).get("SEVERITY.HIGH") == 0, "High-severity static finding exists")
    for name in ("bandit.json", "ruff-undefined-names.json", "static-triage.json"):
        evidence.bind("artifacts/security/" + name)
    return {"selected_graph_packages_with_advisories": 0, "npm_vulnerabilities": 0, "source_exposure_findings": 0, "optional_constraints_inventory": {key: value for key, value in audit.get("groups", {}).items() if "constraints-inventory" in key}, "static_review": static, "scope": "Recorded exact-version package graphs and reviewed source findings; not an OS/container or all-optional-extras zero-advisory claim."}


def optional_os(evidence, path):
    if not path or not (ROOT / path).is_file():
        return {"status": "not_run", "findings": None, "scope": "No verified OS scan supplied. Package-graph audit results are not an OS scan."}
    value = evidence.read(path)
    if isinstance(value.get("images"), list) and value.get("passed_execution") is True:
        folder = repo_path(path).parent
        def sibling(name):
            return (folder / name).relative_to(ROOT).as_posix()
        provenance = evidence.read(sibling("scanner-provenance.json"))
        require(provenance == value.get("scanner"), "OS scanner/database provenance differs")
        require(provenance.get("scanner_identity") == {"docker_socket_readable": False, "docker_socket_writable": False, "uid": 65534}, "OS scanner isolation proof differs")
        packages = evidence.read(sibling("runtime-package-evidence.json"))
        expected = {"orderflow-api:latest", "orderflow-worker:latest", "orderflow-init:latest", "orderflow-batch:latest", "orderflow-postgres:17.12-security"}
        require(set(packages.get("runtime", {})) == expected, "Selected runtime image inventory differs")
        context = evidence.read("artifacts/security/docker-final-context.json")
        for name in ("Dockerfile", "Dockerfile.batch", "infra/Dockerfile.postgres", "docker-compose.yml"):
            evidence.bind(name, {"sha256": context.get("files", {}).get(name)})
        smoke = evidence.read(sibling("docker-final-os-smoke.json"))
        require(smoke.get("passed") is True and smoke.get("cleanup_exit_code") == 0, "Final container smoke/cleanup failed")
        current_map(smoke.get("source_files"))
        selected, reference = [], []
        def finding_key(item):
            return tuple(item.get(key) or "" for key in ("VulnerabilityID", "PkgName", "InstalledVersion", "FixedVersion", "Severity"))
        for image in value["images"]:
            info = image.get("image", {})
            tags = set(info.get("tags", []))
            chosen = tags & expected
            require(len(chosen) <= 1, "One image ambiguously represents multiple selected roles")
            name = image.get("report_file", "")
            require(name and Path(name).name == name and "\\" not in name, "Unsafe OS raw-report filename")
            raw_path = sibling("raw/" + name)
            evidence.bind(raw_path, {"sha256": image.get("report_sha256")})
            raw = evidence.read(raw_path)
            require(isinstance(raw.get("Results"), list) and raw.get("Metadata", {}).get("OS") == image.get("detected_os"), "Raw OS report/platform differs")
            findings = [finding for result in raw["Results"] for finding in result.get("Vulnerabilities", [])]
            require(sorted(map(finding_key, findings)) == sorted(map(finding_key, image.get("findings", []))), "Raw and summarized OS findings differ")
            counts = {severity: sum(item.get("Severity") == severity for item in findings) for severity in {item.get("Severity") for item in findings}}
            fixes = sum(bool(item.get("FixedVersion")) for item in findings)
            require(counts == image.get("severity_counts") and fixes == image.get("fixed_version_available"), "OS severity/fix totals differ")
            item = {"tags": sorted(tags), "image_id": info.get("image_id"), "trivy_config_id": raw.get("Metadata", {}).get("ImageID"), "archive_sha256": image.get("archive_sha256"), "report": raw_path, "findings": len(findings), "severity_counts": counts, "fixed_versions_available": fixes}
            if chosen:
                tag = next(iter(chosen))
                require(packages["runtime"][tag].get("image_id") == info.get("image_id"), "OS package inventory/image differs")
                if tag in {"orderflow-api:latest", "orderflow-worker:latest"}:
                    require(smoke.get(tag.split("-")[1].split(":")[0] + "_image_id") == info.get("image_id"), "Container smoke/scanned image differs")
                selected.append(item)
            else:
                require(tags == {"postgres:17.12"}, "Unexpected unselected OS image")
                reference.append({**item, "classification": "reference_only_not_selected_runtime"})
        require(len(selected) == 5 and {tag for item in selected for tag in item["tags"]} == expected, "OS scan lacks the five distinct selected runtime images")
        return {"status": "scanned", "findings": sum(item["findings"] for item in selected), "selected_images": selected, "reference_images": reference, "scanner": provenance, "captured_at": value.get("captured_at"), "scope": "OS package/advisory pairs in five exact selected runtime images; repeated Python-image findings are not unique vulnerabilities. Reference Debian PostgreSQL is retained separately; nonzero and unfixed findings remain visible."}
    if isinstance(value.get("Results"), list) and value.get("Metadata", {}).get("OS"):
        vulnerabilities = [finding for result in value["Results"] for finding in result.get("Vulnerabilities", [])]
        return {"status": "scanned", "findings": len(vulnerabilities), "image": value.get("Metadata", {}).get("ImageID"), "scope": "Raw supplied scanner report; a nonzero result is not hidden by package-graph counts."}
    return {"status": value.get("status", "unverified"), "findings": None, "reason": value.get("reason", "Supplied file does not prove a completed OS scan")}


def optional_ci(evidence, path):
    if not path or not (ROOT / path).is_file():
        return {"status": "blocked", "passed": None, "basis": "Owner-reported account restriction; no successful remote acceptance-run artifact supplied. A workflow file is not a green run."}
    value = evidence.read(path)
    url = value.get("html_url", value.get("url", ""))
    success = value.get("conclusion") == "success" and bool(value.get("id", value.get("run_id"))) and url.startswith("https://github.com/") and "/actions/runs/" in url
    return {"status": "passed" if success else value.get("status", "blocked"), "passed": True if success else None, "conclusion": value.get("conclusion"), "url": url or None, "reason": value.get("reason")}


def bind_raw(evidence, items):
    require(isinstance(items, list) and items, "Missing raw/input artifacts")
    for item in items:
        path = repo_path(item["path"])
        require(path.is_relative_to((ROOT / "artifacts/benchmarks/raw").resolve()), "Benchmark raw file is outside the bounded raw directory")
        evidence.bind(item["path"], item)
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    pass  # Validate full gzip/CRC, not merely the header or filename.


def benchmark_harness():
    return {name: sha(repo_path(name)) for name in ("scripts/benchmark.py", "scripts/verification_support.py", "fixtures/seed_metadata.json", "uv.lock")}


def verify_trial(evidence, trial, source, harness):
    require(trial.get("passed") is True and trial.get("owned_database_removed") is True and trial.get("http_errors") == [], "Failed, interrupted, or uncleaned trial")
    require(trial.get("source") == source and trial.get("harness_sha256") == harness, "Trial source/harness differs")
    reconciliation = trial.get("reconciliation", {})
    checks = reconciliation.get("checks", [])
    require(reconciliation.get("passed") is True and checks and all(check.get("passed") is True for check in checks), "Independent reconciliation is missing/failed")
    require(trial.get("terminal_receipts_per_second", 0) > 0 and trial.get("distinct_accepted_ids", 0) > 0, "Trial measurements are incomplete")
    bind_raw(evidence, trial.get("raw_artifacts"))


def verify_benchmarks(evidence, small_path, large_path):
    if not small_path and not large_path:
        return {"status": "not_verified", "metrics": None, "reason": "No explicit final summaries selected; historical/prototype runs are not promoted."}
    require(bool(small_path) and bool(large_path), "Select both final small and large summaries, or neither")
    source, harness = core_snapshot(), benchmark_harness()
    small = evidence.read(small_path)
    require(small.get("schema") == "orderflow-core-benchmark-v1" and small.get("complete") is True and small.get("passed") is True, "Small gate is failed/incomplete")
    require(small.get("source") == source and small.get("harness_sha256") == harness, "Small gate is stale")
    runs = small.get("runs", [])
    require(len(runs) == 6 and {(run.get("workers"), run.get("repetition")) for run in runs} == PAIRS, "Small gate does not contain all six actual trials")
    bind_raw(evidence, small.get("inputs"))
    for run in runs:
        verify_trial(evidence, run, source, harness)
    small_sha = sha(repo_path(small_path))
    large = evidence.read(large_path)
    require(large.get("schema") == "orderflow-parallel-large-series-v1" and large.get("complete") is True and large.get("passed") is True and large.get("issues") == [], "Large summary is failed/incomplete")
    require(large.get("source") == source and large.get("harness_sha256") == harness, "Large summary is stale")
    wrapper_sha = sha(ROOT / "scripts/benchmark_trial.py")
    require(large.get("wrapper_sha256") == wrapper_sha, "Large trial wrapper differs")
    require(large.get("small_gate", {}).get("sha256") == small_sha, "Large run did not use this exact passing small gate")
    bind_raw(evidence, large.get("inputs"))
    reports = large.get("actual_reports", [])
    require(len(reports) == 6 and {(report.get("workers"), report.get("repetition")) for report in reports} == PAIRS, "Large summary lacks six actual report files")
    large_runs, sandboxes = [], []
    for item in reports:
        evidence.bind(item["path"], item)
        value = evidence.read(item["path"])
        require(value.get("schema") == "orderflow-single-large-trial-v1" and value.get("passed") is True, "Large raw report failed")
        require(value.get("source") == source and value.get("harness_sha256") == harness and value.get("wrapper_sha256") == wrapper_sha, "Large source/wrapper/harness differs")
        require(value.get("gate", {}).get("sha256") == small_sha, "Raw large trial used another gate")
        require((value.get("workers"), value.get("repetition")) == (item["workers"], item["repetition"]), "Large identity differs from summary")
        bind_raw(evidence, value.get("inputs"))
        trial = value.get("trial", {})
        verify_trial(evidence, trial, source, harness)
        require(trial.get("distinct_accepted_ids") == value.get("scale") and value.get("scale", 0) > small.get("mix", {}).get("distinct_accepted", 0), "Large actual scale differs")
        large_runs.append({**trial, "workers": value["workers"], "repetition": value["repetition"]})
        sandboxes.append(value.get("declared_sandbox", {}).get("name"))
    require(all(sandboxes) and len(set(sandboxes)) == 6, "Parallel topology does not identify six separately owned trial VMs")
    def groups(values):
        result = {}
        for workers in (1, 4):
            selected = [run for run in values if run["workers"] == workers]
            rates = [run["terminal_receipts_per_second"] for run in selected]
            result[str(workers)] = {"actual_trials": len(selected), "terminal_receipts_per_second": rates, "median_terminal_receipts_per_second": statistics.median(rates), "per_trial_p95_acceptance_ms": [run.get("acceptance_latency_ms", {}).get("p95") for run in selected], "per_trial_p95_observed_visibility_ms": [run.get("observed_terminal_visibility_latency_ms", {}).get("p95") for run in selected]}
        return result
    return {"status": "verified", "small": {"artifact": small_path, "scale": small["mix"]["distinct_accepted"], "groups": groups(runs)}, "large": {"artifact": large_path, "scale": large["mix"]["distinct_accepted"], "groups": groups(large_runs)}, "scope": large.get("topology"), "latency_definition": small.get("latency_definition"), "limitations": large.get("limitations", [])}


def build_manifest(args):
    evidence = Evidence()
    source = snapshot()
    native = evidence.gate("native_full_suite", lambda: verify_proof(evidence, args.native_xml, expected_platform="windows"))
    linux = evidence.gate("linux_full_suite", lambda: verify_proof(evidence, args.linux_xml, expected_platform="linux"))
    evidence.gate("same_full_suite", lambda: require(native is not None and linux is not None and native["counts"]["tests"] == linux["counts"]["tests"], "Native/Linux full-suite counts differ or are unverified"))
    evidence.gate("independent_fixture_oracle", lambda: verify_harness(evidence, "acceptance", "artifacts/checks/acceptance.json", 89))
    evidence.gate("process_recovery", lambda: verify_harness(evidence, "recovery", "artifacts/checks/recovery.json", 17))
    evidence.gate("backup_restore", lambda: verify_harness(evidence, "backup", "artifacts/checks/backup-restore.json"))
    evidence.gate("browser_isolated", lambda: verify_browser(evidence, "artifacts/dashboard/browser-results.json", 18, 25))
    evidence.gate("browser_actual_http", lambda: verify_browser(evidence, "artifacts/dashboard/live/live-results.json", 10, 22))
    evidence.gate("selected_package_and_source_audit", lambda: verify_security(evidence))
    benchmarks = evidence.gate("benchmark_selection", lambda: verify_benchmarks(evidence, args.small_benchmark, args.large_benchmark))
    os_scan = optional_os(evidence, args.os_report)
    ci = optional_ci(evidence, args.ci_report)
    require(snapshot() == source, "Source changed while building release manifest")
    selected = {args.small_benchmark, args.large_benchmark}
    historical = [{"path": path.relative_to(ROOT).as_posix(), "sha256": sha(path), "classification": "historical_or_prototype_not_a_release_claim"} for path in sorted((ROOT / "artifacts/benchmarks").glob("*.json")) if path.relative_to(ROOT).as_posix() not in selected]
    return {"schema": "orderflow-release-manifest-v1", "release": "v0.1.0", "generated_at": datetime.now(timezone.utc).isoformat(), "passed": all(gate["passed"] for gate in evidence.gates.values()), "gates": evidence.gates, "test_inputs": source, "core_and_sql": core_snapshot(), "evidence_files": list(evidence.files.values()), "benchmarks": benchmarks, "historical_benchmarks": historical, "os_scan": os_scan, "remote_ci": ci, "configuration": vars(args), "limitations": ["Synthetic local/container evidence is distinct from hosted production capacity, customer effects, or a service SLO.", "Selected dependency graphs have their own advisory result. Optional constraint inventories and OS findings are not silently converted to zero.", "Source-exposure/static reports retain their actual generated times and scan scopes. Artifact hashing binds bytes; it does not invent an execution event.", "Automated accessibility scans cover the recorded 47 states; they are not full WCAG conformance.", "Cloud/Neon account terms and private source visibility remain owner decisions. The release tooling does not publish, push, reset serving data, or upgrade dependencies."]}


def verify_manifest(value):
    require(value.get("schema") == "orderflow-release-manifest-v1" and value.get("release") == "v0.1.0" and value.get("passed") is True, "Manifest is not a passing v0.1.0 local release gate")
    require(value.get("test_inputs") == snapshot() and value.get("core_and_sql") == core_snapshot(), "Manifest source/SQL inputs are stale")
    require(value.get("gates") and all(gate.get("passed") is True for gate in value["gates"].values()), "Manifest contains failed gates")
    for item in value.get("evidence_files", []):
        path = repo_path(item["path"])
        require(sha(path) == item["sha256"] and path.stat().st_size == item["bytes"], "Manifest artifact changed: " + item["path"])
    require(bool(value.get("evidence_files")), "Manifest has no bound artifacts")
    return True


def main(argv=None):
    if not __debug__ or sys.flags.optimize:
        raise RuntimeError("Release verification requires non-optimized Python")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-xml", default="artifacts/checks/full-suite.xml")
    parser.add_argument("--linux-xml", default="artifacts/checks/linux-full-suite.xml")
    parser.add_argument("--small-benchmark")
    parser.add_argument("--large-benchmark")
    parser.add_argument("--os-report")
    parser.add_argument("--ci-report")
    parser.add_argument("--output", default="artifacts/release_manifest.json")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    value = build_manifest(args)
    if not args.check_only:
        output = safe_artifact(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": value["passed"], "release": value["release"], "gates": {name: {"passed": gate["passed"], **({"error": gate["error"]} if "error" in gate else {})} for name, gate in value["gates"].items()}, "os_scan": value["os_scan"], "remote_ci": value["remote_ci"], "benchmarks": (value.get("benchmarks") or {}).get("status")}, indent=2))
    return 0 if value["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
