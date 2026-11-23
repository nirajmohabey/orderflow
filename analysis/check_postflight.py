"""One isolated real-PG/HTTP proof of expired versus renewed fixture credentials.

This is analysis evidence, outside the selected 104-test release scope. It does
not reproduce or relabel the deleted database of the failed v1 large trial.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_tests import snapshot
from verification_support import scratch_database


def main():
    if not __debug__ or sys.flags.optimize:
        raise RuntimeError("Verification requires assertions enabled")
    before = snapshot()
    checks = []
    statuses = {}
    fixture = None
    process = None
    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise RuntimeError("Postflight proof failed: " + name)
    with scratch_database("oracle") as fixture, tempfile.TemporaryFile() as log:
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "orderflow.api:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "warning"], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Owned proof API exited before readiness")
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError("Owned proof API readiness timeout")
                session = client.post("/v1/dev/session", json={"identity_id": "alpha-operator"})
                check("real fixture sign-in succeeds", session.status_code == 200)
                secret = fixture["env"]["ODF_AUTH_SECRET"]
                claims = jwt.decode(session.json()["access_token"], secret, algorithms=["HS256"], audience=fixture["env"]["ODF_JWT_AUDIENCE"], issuer=fixture["env"]["ODF_JWT_ISSUER"])
                now = int(time.time())
                claims.update(iat=now - 1900, exp=now - 100)
                expired = jwt.encode(claims, secret, algorithm="HS256")
                response = client.get("/v1/overview", headers={"Authorization": "Bearer " + expired})
                statuses["expired_bearer_overview"] = response.status_code
                check("equivalent older-than30min fixture bearer is rejected", response.status_code == 401)
                tokens = {}
                for alias in ("alpha-operator", "beta-operator"):
                    response = client.post("/v1/dev/session", json={"identity_id": alias})
                    statuses[alias + "_fresh_signin"] = response.status_code
                    check(alias + " fresh sign-in succeeds", response.status_code == 200)
                    tokens[alias] = response.json()["access_token"]
                    response = client.get("/v1/overview", headers={"Authorization": "Bearer " + tokens[alias]})
                    statuses[alias + "_fresh_overview"] = response.status_code
                    check(alias + " fresh bearer accesses overview", response.status_code == 200)
                beta = "22222222-2222-4222-8222-222222222222"
                source = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                body = {"tenant_id": beta, "source_event_id": "postflight-proof-beta", "partition_key": "product:POSTFLIGHT-BETA", "schema_version": "1", "entity_type": "product", "entity_id": "POSTFLIGHT-BETA", "entity_version": 1, "occurred_at": "2026-11-01T12:00:00Z", "effective_at": "2026-11-01T12:00:00Z", "payload": {"sku": "POSTFLIGHT-BETA", "name": "Synthetic proof", "unit_price_minor": 100, "currency": "USD"}}
                accepted = client.post(f"/v1/sources/{source}/events", json=body, headers={"X-Source-Key": fixture["source_keys"][source]})
                check("new owned Beta event durably accepted", accepted.status_code == 202)
                path = "/v1/events/" + accepted.json()["event_id"]
                statuses["beta_own_event"] = client.get(path, headers={"Authorization": "Bearer " + tokens["beta-operator"]}).status_code
                statuses["alpha_cross_tenant_event"] = client.get(path, headers={"Authorization": "Bearer " + tokens["alpha-operator"]}).status_code
                check("fresh Beta token reads the actual existing event", statuses["beta_own_event"] == 200)
                check("fresh Alpha token receives404 for actual Beta event", statuses["alpha_cross_tenant_event"] == 404)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    after = snapshot()
    check("selected104-test source inputs unchanged", before == after)
    check("only the fresh owned verification database was removed", fixture["removed"])
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "passed": all(item["passed"] for item in checks), "checks": checks, "http_statuses": statuses, "selected_source_sha256": before["sha256"], "selected_source_unchanged": before == after, "owned_database_removed": fixture["removed"], "proof_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "future_harness_sha256": hashlib.sha256((ROOT / "analysis/benchmark_v2.py").read_bytes()).hexdigest(), "scope": "One separate native real-PostgreSQL/loopback-HTTP expiry/refresh/tenant proof, not an added test in the selected104 suite or a rerun of100k.", "limitations": ["Old v1 reports retain their actual false postflight booleans; the missing contemporaneous HTTP statuses are not reconstructed.", "This bounded reproduction establishes expiry and fresh-login behavior; it does not promote a failed or incomplete benchmark matrix.", "The expired synthetic token preserves the real30min lifetime but backdates issuance/expiry; system clocks and auth source are unchanged."]}
    output = ROOT / "artifacts/benchmarks/protocol/postflight-expiry-proof.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(checks), "http_statuses": statuses, "path": output.relative_to(ROOT).as_posix()}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
