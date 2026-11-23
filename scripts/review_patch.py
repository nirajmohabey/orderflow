"""Collect current native v0.1.1 proof; preserve unavailable and historical gates."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tomllib

from release_manifest import Evidence, current_map, require, verify_proof
from run_tests import ROOT, core_snapshot, sha, snapshot

OUT = ROOT / "artifacts/recheck-2026-11-23"
MANIFEST = OUT / "review-manifest-v0.1.1.json"
UI_DEFAULT = ROOT.parents[1] / "work/recheck-orderflow/v0.1.1"
FRONTEND = {"src/App.tsx", "src/api.ts", "src/types.ts", "src/style.css", "src/main.tsx", "package.json", "package-lock.json", "index.html"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def record(path, status=None):
    result = {"path":path.relative_to(ROOT).as_posix(), "sha256":sha(path), "bytes":path.stat().st_size}
    if status:
        result["status"] = status
    return result


def bind(evidence, path):
    return evidence.bind(path.relative_to(ROOT).as_posix())


def copy_evidence(evidence, source, relative):
    target = OUT / relative
    digest = sha(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    require(sha(source) == sha(target) == digest, "Evidence changed during copy: " + relative)
    return bind(evidence, target)


def browser(evidence, ui, name, count, scans=None):
    data = read(ui / name)
    require(data.get("passed") == data.get("total") == count and data.get("source_stable") is True, "Browser counts/source failure: " + name)
    require(len(data.get("results", [])) == count and all(r.get("passed") is True and not r.get("pageerrors") for r in data["results"]), "Browser scenario failure: " + name)
    require(not data.get("pageerrors") and not data.get("console_errors"), "Browser errors: " + name)
    hashes = data.get("source_hashes", {})
    require(FRONTEND <= set(hashes), "Frontend source binding incomplete: " + name)
    current_map({"dashboard/" + key:value for key,value in hashes.items()})
    states = data.get("accessibility", [])
    if scans is not None:
        require(len(states) == scans, "Accessibility state count differs: " + name)
    require(not any(state.get("violations") for state in states), "Accessibility violation: " + name)
    copy_evidence(evidence, ui / name, "ui/" + name)
    return {"checks":count, "accessibility_states":len(states), "violations":0,
            "incomplete_entries":sum(len(s.get("incomplete", [])) if isinstance(s.get("incomplete"),list) else int(s.get("incomplete",0)) for s in states), "mode":data.get("mode")}


def build(ui):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    require(version == "0.1.1", "This collector is versioned for v0.1.1")
    source = snapshot()
    evidence = Evidence()
    native_path = (OUT / "native/full-suite.xml").relative_to(ROOT).as_posix()
    native = verify_proof(evidence, native_path, expected_platform="windows")
    harnesses = {}
    for kind, filename, expected in (("acceptance","acceptance.json",89),("recovery","recovery.json",17),("backup","backup-restore.json",None)):
        path = OUT / "native" / filename
        verify_proof(evidence, path.relative_to(ROOT).as_posix(), kind, expected_platform="windows")
        data = read(path)
        require(data.get("passed") is True, "Native harness failed: " + kind)
        if expected is not None:
            require(len(data.get("checks",[])) == expected and all(c.get("passed") is True for c in data["checks"]), "Native harness check count/failure: " + kind)
        if kind in {"acceptance","recovery"}:
            require(data.get("owned_database_removed") is True, "Harness owned database remains: " + kind)
        if kind == "acceptance":
            require(data.get("configured_database_reset") is False, "Acceptance reset serving data")
        if kind == "backup":
            require(data.get("source_database_written") is False and data.get("owned_restore_removed") is True and data.get("private_dump_removed") is True, "Backup source/restore/dump preservation failed")
            require(data.get("table_count") == len(data.get("table_hashes",{})) == 23 and data.get("duplicate_replays") == 14 and data.get("new_worker_effects") == 0, "Backup exact table/replay/effect checks differ")
        harnesses[kind] = {"status":"current_native_passed", "checks":expected, "artifact":record(path)}

    dependencies = read(OUT / "dependencies/audit-summary.json")
    require(dependencies.get("required_graph_passed") is True and dependencies.get("required_graph_packages_with_advisories") == 0 and dependencies.get("missing_required_graphs") == [], "Required dependency graph failed/incomplete")
    require(dependencies["npm"]["exit_code"] == dependencies["npm"]["reported_vulnerabilities"] == 0, "npm audit failed")
    for name, info in dependencies["inputs"].items():
        evidence.bind(name, info)
    osv = read(OUT / "dependencies/osv-audit.json")
    require(osv["unique_exact_versions"] == dependencies["unique_exact_versions"] == len(osv["raw_batch_results"]), "OSV query coverage differs")
    require(all(isinstance(row,dict) and not row.get("error") and not row.get("next_page_token") for row in osv["raw_batch_results"]), "OSV query failed or incomplete")
    npm = read(OUT / "dependencies/npm-audit.json")
    require(npm.get("metadata",{}).get("vulnerabilities",{}).get("total") == 0, "Raw npm finding differs")
    for path in sorted((OUT / "dependencies").glob("*.json")):
        bind(evidence,path)

    security = OUT / "security"
    exposure = read(security / "source-exposure.json")
    require(exposure.get("findings") == [] and exposure.get("text_files_scanned",0) > 0, "Source exposure findings")
    static = read(security / "static-audit-summary.json")
    require(static.get("passed") is True and static.get("ruff_undefined_names") == 0 and static.get("unreviewed_medium_findings") == [], "Static gate incomplete/failed")
    require(static.get("bandit_severities",{}).get("SEVERITY.HIGH") == 0 and static.get("bandit_findings_suppressed") == 0, "High/suppressed static findings")
    provenance = read(security / "provenance.json")
    require(provenance.get("source_before") == provenance.get("source_after") == source, "Security scan source binding stale")
    names = ("source-exposure.json","bandit.json","ruff-undefined-names.json","static-triage.json","static-audit-summary.json")
    for name in names:
        require(provenance.get("reports",{}).get(name) == sha(security / name), "Security report changed: " + name)
        bind(evidence,security / name)
    bind(evidence,security / "provenance.json")
    require(read(security / "bandit.json").get("errors") == [] and read(security / "ruff-undefined-names.json") == [], "Raw static errors/findings differ")

    browser_results = {
        "isolated_existing":browser(evidence,ui,"mock-existing/browser-results.json",18,25),
        "isolated_regressions":browser(evidence,ui,"mock-regressions/browser-results.json",5),
        "actual_http":browser(evidence,ui,"live/live-results.json",10,22),
    }
    post = read(ui / "postchecks.json")
    require(post.get("passed") is True and post.get("source_stable") is True and post.get("before_source_hashes") == post.get("after_source_hashes"), "UI source changed during run")
    current_map(post["after_source_hashes"])
    require({"dashboard/"+name for name in FRONTEND} <= set(post["after_source_hashes"]), "UI final frontend source binding incomplete")
    for name in ("mock-existing/browser-results.json","mock-regressions/browser-results.json","live/live-results.json"):
        require(post.get("reports",{}).get(name) == sha(ui / name), "UI postcheck report hash mismatch: " + name)
    isolation = read(ui / "owned-live-isolation.json")
    require(isolation.get("phase") == "completed" and isolation.get("passed") is True and isolation.get("serving_database_preserved") is True, "Owned live review incomplete or serving state changed")
    require(isolation.get("source_reset") is False and isolation.get("user_process_records_written") is False, "Live review reset source/runtime records")
    require(isolation.get("owned_api_stopped") is True and isolation.get("owned_database_removed") is True and isolation.get("owned_api_port_closed") is True, "Owned UI runtime not cleaned")
    require(isolation.get("env_sha256_before") == isolation.get("env_sha256_after") and isolation.get("env_sha256_after"), "Existing env changed")
    require(isolation.get("user_process_record_hashes_before") == isolation.get("user_process_record_hashes_after") and isolation.get("user_process_record_hashes_after"), "Existing process records changed")
    require(isolation.get("source_tables_before") == isolation.get("source_tables_after") and len(isolation.get("source_tables_after",{})) == 23, "Serving23table snapshot changed/incomplete")
    for name in ("postchecks.json","owned-live-isolation.json"):
        copy_evidence(evidence,ui / name,"ui/" + name)
    # Optional media and additional read-only proof are bound, not counted as journeys.
    for path in sorted(ui.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".png",".webm",".mp4"}:
            copy_evidence(evidence,path,"ui/" + path.relative_to(ui).as_posix())
    historical_names = ("artifacts/release_manifest.json","artifacts/checks/linux-full-suite.xml","artifacts/checks/linux-full-suite.source-proof.json","artifacts/analysis/summary.json","artifacts/security/container-os-audit/os-audit.json","artifacts/security/container-os-audit/docker-final-os-smoke.json")
    historical = [record(ROOT / name,"historical_v0.1.0_not_current_source") for name in historical_names]
    old = read(ROOT / "artifacts/release_manifest.json")
    require(old.get("benchmarks",{}).get("status") == "not_verified", "Historical benchmark must remain not_verified")
    partial = old.get("supplementary_partial_evidence",{})
    require(partial.get("status") == "partial" and partial.get("passed") is False and partial.get("large_passed") == 5 and partial.get("large_failed") == 1, "Original failed benchmark classification changed")
    require(snapshot() == source, "Source changed during collection")
    value = {
        "schema":"orderflow-patch-review-v1", "release":"v0.1.1", "generated_at":datetime.now(timezone.utc).isoformat(),
        "current_checks_passed":True, "verification_scope":"Current native Windows review; current Linux/hosted/remoteCI/full benchmark were not verified.",
        "test_inputs":source, "core_and_sql":core_snapshot(), "native_full_suite":native,
        "native_harnesses":harnesses, "browser":browser_results, "browser_checks_total":33,
        "dependency_audit":dependencies, "source_exposure":exposure, "static_analysis":static,
        "benchmarks":{"status":"not_verified", "current_full_v2_matrix_executed":False, "historical_v1":{"small_passed":6,"large_passed":5,"large_failed":1,"scope":"Original reports retained; not a passing100k gate or current-source performance result."}},
        "unavailable":{"current_linux":"not_run_free_cloud_quota_exhausted_no_paid_upgrade", "current_container":"not_rebuilt_or_retested", "current_os_image_scan":"not_run_old_exact_image_findings_are_historical", "hosted_backend":"not_verified", "remote_ci":"not_verified", "current_linux_airflow_execution":"not_run_historical_inventory_only_requeried_for_advisories"},
        "historical_evidence":historical, "evidence_files":list(evidence.files.values()),
        "limitations":["Synthetic local fixture evidence does not establish hosted readiness, customer effects or production reliability.","Optional Airflow constraints retain their disclosed advisories; zero required-graph matches is a separate scope.","Historical OS image scans retain nonzero/unfixed findings; application advisory counts do not erase them.","Automated accessibility scans and selected browser states do not establish complete WCAG conformance.","Future-v2 mocked/CPU regressions do not execute or promote a10k/100k performance matrix."],
    }
    MANIFEST.parent.mkdir(parents=True,exist_ok=True)
    MANIFEST.write_text(json.dumps(value,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    verify()
    return value


def verify():
    value = read(MANIFEST)
    require(value.get("schema") == "orderflow-patch-review-v1" and value.get("release") == "v0.1.1" and value.get("current_checks_passed") is True, "Not the expected native patch review")
    require(value["test_inputs"] == snapshot() and value["core_and_sql"] == core_snapshot(), "Review source snapshot stale")
    require(value["benchmarks"]["status"] == "not_verified", "Benchmark gate was promoted")
    for item in [*value["evidence_files"],*value["historical_evidence"]]:
        path = (ROOT / item["path"]).resolve()
        require(path.is_relative_to(ROOT) and sha(path) == item["sha256"] and path.stat().st_size == item["bytes"], "Review evidence changed: " + item["path"])
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-work-dir",type=Path,default=UI_DEFAULT)
    parser.add_argument("--verify",action="store_true")
    args=parser.parse_args()
    value=verify() if args.verify else build(args.ui_work_dir.resolve())
    print(json.dumps({"manifest":str(MANIFEST),"release":value["release"],"native_tests":value["native_full_suite"]["counts"],"browser_checks":value["browser_checks_total"],"benchmark":"not_verified","linux":"not_run"},indent=2))


if __name__ == "__main__":
    main()
