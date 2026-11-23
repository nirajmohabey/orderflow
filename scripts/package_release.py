"""Package a committed Git HEAD plus explicitly hash-bound release evidence.

This script never commits, pushes, publishes, resets a DB, or archives runtime
directories. Imported helpers have no execution side effects.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from release_manifest import ROOT, VerificationError, require, verify_manifest
from run_tests import sha

FORBIDDEN = {".git", ".runtime", ".vercel", "node_modules", "__pycache__", ".pytest_cache", ".aws", ".ssh"}


def member_allowed(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and "\\" not in name and ":" not in name and ".." not in path.parts, "Unsafe archive path: " + name)
    require(path.parts and path.parts[0] == "orderflow", "Archive member lacks the project prefix")
    for part in path.parts:
        require(part not in FORBIDDEN and not part.startswith((".venv", ".env")), "Private/runtime archive member: " + name)
    require(path.suffix not in {".pyc", ".pid"}, "Private compiled/process member: " + name)
    return True


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, stderr=subprocess.PIPE)


def file_record(path):
    return {"bytes": path.stat().st_size, "sha256": sha(path)}


def verify_zip(path):
    records = {}
    with zipfile.ZipFile(path) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP member")
        for item in archive.infolist():
            member_allowed(item.filename)
            require(not stat.S_ISLNK(item.external_attr >> 16), "Symlink archive member is forbidden")
            if item.is_dir():
                continue
            value = hashlib.sha256()
            with archive.open(item) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    value.update(chunk)
            records[item.filename] = {"bytes": item.file_size, "sha256": value.hexdigest()}
        require(archive.testzip() is None, "ZIP CRC verification failed")
    return records


def main(argv=None):
    if not __debug__ or sys.flags.optimize:
        raise RuntimeError("Packaging requires assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="artifacts/release_manifest.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT.parent)
    args = parser.parse_args(argv)
    manifest_path = (ROOT / args.manifest).resolve()
    require(manifest_path.is_relative_to(ROOT), "Manifest path escapes project")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_manifest(manifest)
    require(git("rev-parse", "--show-toplevel").decode().strip().replace("\\", "/").lower() == str(ROOT).replace("\\", "/").lower(), "OrderFlow must be its own Git checkout")
    head = git("rev-parse", "HEAD").decode().strip()
    require(len(head) == 40, "No committed Git HEAD exists")
    require(not git("diff", "--name-only", "HEAD").strip(), "Tracked files changed after commit; root must review and commit before packaging")
    # A clean index does not include untracked source. Check every claimed input
    # directly against Git HEAD rather than quietly substituting working files.
    protected = dict(manifest["test_inputs"]["files"])
    for item in manifest["evidence_files"]:
        if not item["path"].startswith("artifacts/"):
            protected[item["path"]] = item["sha256"]
    for name, wanted in protected.items():
        try:
            actual = hashlib.sha256(git("show", "HEAD:" + name)).hexdigest()
        except subprocess.CalledProcessError:
            raise VerificationError("Release input is not committed: " + name) from None
        require(actual == wanted, "Git HEAD differs from verified release input: " + name)
    destination = args.output_dir.resolve()
    require(destination == ROOT.parent.resolve(), "Deliverables must use the named outputs directory")
    demo = destination / "OrderFlow_Demo.mp4"
    require(demo.is_file() and demo.stat().st_size > 32, "Selected OrderFlow_Demo.mp4 is absent")
    require(b"ftyp" in demo.read_bytes()[:32], "Demo is not an MP4 container")
    preview = destination / "OrderFlow_Preview.png"
    preview_source = ROOT / "artifacts/dashboard/live/06-clean-overview-viewport.png"
    require(preview_source.is_file(), "Final viewport preview is missing")
    shutil.copy2(preview_source, preview)
    require(preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "Preview is not PNG")
    output = destination / "OrderFlow_Complete.zip"
    extras = {item["path"]: ROOT / item["path"] for item in manifest["evidence_files"]}
    extras[manifest_path.relative_to(ROOT).as_posix()] = manifest_path
    extras["media/OrderFlow_Demo.mp4"] = demo
    extras["media/OrderFlow_Preview.png"] = preview
    # Raw benchmark gzip and input ledgers are included only when selected final
    # benchmark validation bound them. Ignored runtime/private object roots stay out.
    verification = {
        "schema": "orderflow-package-verification-v1", "release": "v0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(), "git_head": head,
        "manifest_sha256": sha(manifest_path), "core_and_sql_sha256": manifest["core_and_sql"]["sha256"],
        "test_inputs_sha256": manifest["test_inputs"]["sha256"], "local_gate_passed": True,
        "benchmark_status": (manifest.get("benchmarks") or {}).get("status"),
        "os_scan": manifest["os_scan"], "remote_ci": manifest["remote_ci"],
        "media": {demo.name: file_record(demo), preview.name: file_record(preview)},
        "private_runtime_excluded": True, "source": "git archive HEAD; no working-tree source substitution",
    }
    # Replace the deliverable only after the complete temporary archive verifies.
    with tempfile.TemporaryDirectory(prefix="orderflow-release-", dir=destination) as folder:
        temporary = Path(folder) / "OrderFlow_Complete.zip"
        subprocess.run(["git", "archive", "--format=zip", "--prefix=orderflow/", "--output=" + str(temporary), head], cwd=ROOT, check=True)
        verify_zip(temporary)
        with zipfile.ZipFile(temporary, "a", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            existing = set(archive.namelist())
            for relative, path in sorted(extras.items()):
                name = "orderflow/" + relative
                member_allowed(name)
                if name in existing:
                    require(hashlib.sha256(archive.read(name)).hexdigest() == sha(path), "Committed artifact differs from selected current evidence: " + relative)
                else:
                    archive.write(path, name, compress_type=zipfile.ZIP_STORED if path.suffix in {".gz", ".mp4", ".png"} else zipfile.ZIP_DEFLATED)
                    existing.add(name)
        records = verify_zip(temporary)
        verification["verified_content_files"] = len(records)
        verification["content_hashes"] = records
        with zipfile.ZipFile(temporary, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            require("orderflow/PACKAGE_VERIFICATION.json" not in archive.namelist(), "Reserved verification filename already tracked")
            archive.writestr("orderflow/PACKAGE_VERIFICATION.json", json.dumps(verification, indent=2) + "\n")
        final_records = verify_zip(temporary)
        require(all(final_records[name] == info for name, info in records.items()), "Archive content changed while adding verification")
        verify_manifest(manifest)
        require(git("rev-parse", "HEAD").decode().strip() == head and not git("diff", "--name-only", "HEAD").strip(), "Git HEAD/working files changed during packaging")
        shutil.copy2(temporary, output)
    external = {**verification, "archive": {"name": output.name, **file_record(output)}, "verified_final_files": len(final_records), "verification_note": "Internal content hashes exclude PACKAGE_VERIFICATION.json itself; this external report additionally binds the finished archive."}
    summary = destination / "OrderFlow_Verification.json"
    summary.write_text(json.dumps(external, indent=2) + "\n", encoding="utf-8")
    deliverables = [output, demo, preview, summary]
    checksums = {path.name: file_record(path) for path in deliverables}
    (destination / "OrderFlow_Checksums.json").write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"git_head": head, "archive": output.name, "verified_files": len(final_records), "checksums": checksums}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
