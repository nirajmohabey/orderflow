"""Run real pytest and bind its XML to unchanged, exact release inputs.

No database reset is performed here. The existing tests/harnesses retain their
own verified disposable-destination guards. Importing this module is read-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CORE_NAMES = ("__init__", "config", "db", "schemas", "auth", "service", "worker", "api")
SKIP = {"__pycache__", ".pytest_cache", "node_modules", "target", "logs"}
HARNESS = {
    "acceptance": ("scripts/acceptance.py", "artifacts/checks/acceptance.json"),
    "recovery": ("scripts/recovery_check.py", "artifacts/checks/recovery.json"),
    "backup": ("scripts/backup_check.py", "artifacts/checks/backup-restore.json"),
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path: Path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def safe_artifact(name, root=ROOT):
    path = Path(name)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to((root / "artifacts").resolve()):
        raise ValueError("Evidence output must stay within this project's artifacts directory")
    return path


def snapshot(root=ROOT):
    """Hash code, SQL, dependency inputs, fixtures and tests; never secrets."""
    selected = set()
    for folder in ("src", "migrations", "fixtures", "tests", "scripts", "warehouse", "orchestration"):
        base = root / folder
        if base.is_dir():
            for path in base.rglob("*"):
                relative = path.relative_to(root)
                if path.is_file() and not any(part in SKIP for part in relative.parts):
                    if path.suffix in {".py", ".sql", ".json", ".jsonl", ".csv", ".yml", ".yaml", ".lock", ".in", ".txt", ".ps1"}:
                        selected.add(path)
    for name in ("pyproject.toml", "uv.lock", "requirements.lock", ".python-version", "Dockerfile", "Dockerfile.batch", "docker-compose.yml", "dashboard/package.json", "dashboard/package-lock.json"):
        path = root / name
        if path.is_file():
            selected.add(path)
    files = {path.relative_to(root).as_posix(): sha(path) for path in sorted(selected)}
    if not files or not any(name.startswith("tests/") for name in files):
        raise ValueError("Expected release input/test tree is absent")
    return {"files": files, "sha256": digest(files), "scope_version": "orderflow-test-inputs-v1"}


def core_snapshot(root=ROOT):
    files = {f"src/orderflow/{name}.py": sha(root / f"src/orderflow/{name}.py") for name in CORE_NAMES}
    files.update({path.relative_to(root).as_posix(): sha(path) for path in sorted((root / "migrations").glob("*.sql"))})
    return {"files": files, "sha256": digest(files)}


def xml_counts(path):
    document = ET.parse(path).getroot()
    suites = [element for element in document.iter("testsuite") if not list(element.iter("testsuite"))[1:]]
    if not suites:
        raise ValueError("JUnit XML contains no leaf test suites")
    counts = {key: 0 for key in ("tests", "errors", "failures", "skipped")}
    for suite in suites:
        for key in counts:
            if key not in suite.attrib:
                raise ValueError(f"JUnit suite is missing {key}")
            value = int(suite.attrib[key])
            if value < 0:
                raise ValueError("Negative JUnit count")
            counts[key] += value
    cases = list(document.iter("testcase"))
    if len(cases) != counts["tests"]:
        raise ValueError("JUnit testcase count does not match suite totals")
    counts["time_seconds"] = sum(float(suite.attrib.get("time", "0")) for suite in suites)
    return counts


def complete_default_suite(arguments):
    # Selection/plugin options or file arguments must not masquerade as a full run.
    flags = {"-q", "-qq", "-v", "-vv", "--quiet", "--verbose", "--no-header", "--disable-warnings", "--strict-config", "--strict-markers", "-ra", "-rA", "--color=no", "--color=yes", "--tb=short", "--tb=long", "--tb=auto", "--capture=no", "-s"}
    return all(argument in flags for argument in arguments)


def preserve_previous(paths, root=ROOT):
    existing = [path for path in paths if path.is_file()]
    if existing:
        folder = root / "artifacts/checks/history" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, folder / path.name)


def main(argv=None):
    if not __debug__ or sys.flags.optimize or os.environ.get("PYTHONOPTIMIZE", "") not in {"", "0"}:
        raise RuntimeError("Release tests require assertions enabled; optimized Python is forbidden")
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        raise RuntimeError("Use explicit runner arguments; PYTEST_ADDOPTS could hide test selection")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", "--junit-xml", default="artifacts/checks/full-suite.xml")
    parser.add_argument("--proof")
    parser.add_argument("--label", default=platform.system().lower())
    parser.add_argument("--harness", choices=tuple(HARNESS))
    args, pytest_arguments = parser.parse_known_args(argv)
    if args.harness and pytest_arguments:
        parser.error("Harness runs take no passthrough arguments")
    target = safe_artifact(HARNESS[args.harness][1] if args.harness else args.junitxml)
    proof = safe_artifact(args.proof or str(target.with_suffix(".source-proof.json")))
    if target == proof:
        parser.error("Result and proof paths must differ")
    before, core_before = snapshot(), core_snapshot()
    preserve_previous([target, proof])
    target.parent.mkdir(parents=True, exist_ok=True)
    # A non-created XML/report can never validate a previous successful file.
    prior_hash = sha(target) if target.is_file() else None
    started_wall, started = time.time_ns(), time.monotonic()
    command = ([sys.executable, HARNESS[args.harness][0]] if args.harness else
               [sys.executable, "-m", "pytest", "tests", "--junitxml=" + str(target), *pytest_arguments])
    failure = None
    try:
        exit_code = subprocess.call(command, cwd=ROOT, env=os.environ.copy())
    except (OSError, KeyboardInterrupt) as error:
        exit_code, failure = 130, type(error).__name__
    after, core_after = snapshot(), core_snapshot()
    changed = sorted(name for name in set(before["files"]) | set(after["files"]) if before["files"].get(name) != after["files"].get(name))
    result = None
    fresh = target.is_file() and target.stat().st_mtime_ns >= started_wall
    try:
        if not fresh:
            raise ValueError("No fresh result was written by this invocation")
        if args.harness:
            result = json.loads(target.read_text(encoding="utf-8"))
            successful = result.get("passed") is True
        else:
            result = xml_counts(target)
            successful = result["tests"] > 0 and all(result[key] == 0 for key in ("errors", "failures", "skipped"))
    except (ValueError, OSError, ET.ParseError) as error:
        successful, failure = False, str(error)
    full = not args.harness and complete_default_suite(pytest_arguments)
    passed = exit_code == 0 and successful and not changed and (bool(args.harness) or full)
    record = {
        "schema": "orderflow-test-source-proof-v1", "generated_at": utc(), "label": args.label,
        "kind": args.harness or "pytest", "python": platform.python_version(), "platform": platform.platform(),
        "assertions_enabled": True, "complete_default_suite": full,
        "pytest_arguments": pytest_arguments, "exit_code": exit_code, "elapsed_seconds": time.monotonic() - started,
        "result": {"path": target.relative_to(ROOT).as_posix(), "sha256": sha(target) if target.is_file() else None, "fresh": fresh, "prior_sha256": prior_hash},
        "counts": result if not args.harness else None,
        "source_before": before, "source_after": after, "core_before": core_before, "core_after": core_after,
        "source_changed": bool(changed), "changed_paths": changed, "passed": bool(passed), "failure": failure,
        "scope": "Exact file bytes before and after actual subprocess execution. Existing test/harness destination guards remain authoritative; this runner never resets a database.",
    }
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "kind": record["kind"], "counts": record["counts"], "source_changed": bool(changed), "proof": proof.relative_to(ROOT).as_posix()}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
