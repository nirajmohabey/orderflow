"""Measure the real HTTP/inbox/worker path against a frozen independent ledger.

Raw gzip files contain synthetic event names, UUIDs and timings, never keys.
This measures local core processing, not Airflow/dbt or hosted capacity.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psycopg

from verification_support import ROOT, scratch_database

TENANTS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
SOURCES = ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CORE_FILES = tuple("src/orderflow/" + name + ".py" for name in ("__init__", "config", "db", "schemas", "auth", "service", "worker", "api"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    files = {name: sha(ROOT / name) for name in CORE_FILES}
    files.update({str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in sorted((ROOT / "migrations").glob("*.sql"))})
    return {"files": files, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def harness_manifest():
    return {name: sha(ROOT / name) for name in ("scripts/benchmark.py", "scripts/verification_support.py", "fixtures/seed_metadata.json", "uv.lock")}


def environment_metadata():
    result = {"python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count(), "processor": platform.processor()}
    for name in ("cpu.max", "memory.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result["cgroup_" + name] = path.read_text().strip()
    return result


def packed_json(path, rows):
    # Fixed gzip timestamp makes the input artifact reproducible byte-for-byte.
    with path.open("wb") as file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as zipped:
            for row in rows:
                zipped.write((json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


def dataset(scale, seed):
    """Independent generator: no application imports, hashes or reducers."""
    quarantine_count = scale // 100
    duplicate_revision_count = scale // 20
    base_count = scale - quarantine_count - duplicate_revision_count
    base, additions = [], []
    revisions, current, partition_counts = {}, {}, Counter()
    by_identity = {}
    for number in range(base_count):
        group, version = divmod(number, 4)
        version += 1
        beta = group % 5 == 0
        tenant = TENANTS[1 if beta else 0]
        kind = "product" if beta or group % 2 else "order"
        source = SOURCES[2 if beta else (0 if kind == "product" else 1)]
        entity = ("P-" if kind == "product" else "O-") + f"{group:08d}"
        day = 2 if group % 7 == 0 and version == 3 else version
        effective = f"2026-11-{day:02d}T12:00:00Z"
        payload = ({"sku": "SKU-" + str(group), "name": "Synthetic catalog item", "unit_price_minor": 1000 + group * 17 + version * 50, "currency": "USD"}
                   if kind == "product" else {"customer_id": "C-" + str(group), "total_minor": 1000 + group * 17 + version * 125, "currency": "USD", "status": "paid" if version == 4 else "placed"})
        body = {"tenant_id": tenant, "source_event_id": f"base-{number:09d}", "partition_key": kind + ":" + entity,
                "schema_version": "1", "entity_type": kind, "entity_id": entity, "entity_version": version,
                "effective_at": effective, "occurred_at": effective, "payload": payload}
        row = {"source_id": source, "envelope": body, "category": "base"}
        base.append(row)
        revisions[(tenant, kind, entity, version)] = row
        key = (tenant, kind, entity)
        if key not in current or version > current[key]["envelope"]["entity_version"]:
            current[key] = row
    for number in range(duplicate_revision_count):
        original = base[(number * 17) % base_count]
        row = {"source_id": original["source_id"], "envelope": {**original["envelope"], "source_event_id": f"duplicate-revision-{number:09d}"}, "category": "duplicate_revision"}
        additions.append(row)
    for number in range(quarantine_count):
        beta = number % 5 == 0
        tenant, source = TENANTS[1 if beta else 0], SOURCES[2 if beta else 0]
        entity = f"P-Q-{number:08d}"
        body = {"tenant_id": tenant, "source_event_id": f"quarantine-{number:09d}", "partition_key": "product:" + entity,
                "schema_version": "1", "entity_type": "product", "entity_id": entity, "entity_version": 1,
                "effective_at": "2026-11-06T12:00:00Z", "occurred_at": "2026-11-06T12:00:00Z",
                "payload": {"sku": "Q-" + str(number), "name": "Synthetic wrong-currency record", "unit_price_minor": 1000, "currency": "EUR"}}
        additions.append({"source_id": source, "envelope": body, "category": "quarantined"})
    for row in base + additions:
        body = row["envelope"]
        identity = (body["tenant_id"], row["source_id"], body["source_event_id"])
        by_identity[identity] = row
        partition_counts[(body["tenant_id"], row["source_id"], body["partition_key"])] += 1
    rng = random.Random(seed)
    rng.shuffle(base)
    rng.shuffle(additions)
    accepted_rows = base + additions
    replays = [{**accepted_rows[(n * 37) % scale], "category": "http_replay"} for n in range(scale // 10)]
    rng.shuffle(replays)
    return {"phases": [base, additions, replays], "identities": by_identity, "revisions": revisions,
            "current": current, "partitions": partition_counts, "scale": scale, "seed": seed,
            "mix": {"distinct_accepted": scale, "base_revisions": base_count, "duplicate_revision_events": duplicate_revision_count,
                    "business_quarantines": quarantine_count, "http_replays": len(replays), "http_attempts": scale + len(replays)}}


def identity(row):
    return (row["envelope"]["tenant_id"], row["source_id"], row["envelope"]["source_event_id"])


def utc_string(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def stop_owned(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))]


class VisibilityObserver:
    """Observe committed receipts independently, with server-clock timestamps."""
    def __init__(self, fixture, output, expected):
        self.fixture, self.output, self.expected = fixture, output, expected
        self.seen, self.error = {}, None
        self.stop = threading.Event()
        self.drained = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.poll_ms = []
        self.last_observed_monotonic = None

    def run(self):
        try:
            with psycopg.connect(**self.fixture["kwargs"], dbname=self.fixture["name"], autocommit=True) as conn, gzip.open(self.output, "wt", encoding="utf-8") as raw:
                self.ready.set()
                cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
                while not self.stop.is_set():
                    began = time.perf_counter()
                    for tenant in TENANTS:
                        rows = conn.execute("""SELECT e.tenant_id::text,e.source_id::text,e.source_event_id,e.event_id::text,
                            e.ingested_at,r.outcome,r.committed_at,clock_timestamp()
                            FROM orderflow.processing_receipts r JOIN orderflow.inbox_events e USING(tenant_id,event_id)
                            WHERE r.tenant_id=%s AND r.committed_at>=%s""", (tenant, cutoff)).fetchall()
                        for tenant_id, source, external, event, ingested, outcome, receipt_marker, observed in rows:
                            key = (tenant_id, source, external)
                            if key in self.seen:
                                continue
                            latency_ms = (observed - ingested).total_seconds() * 1000
                            record = {"tenant_id": tenant_id, "source_id": source, "source_event_id": external, "event_id": event,
                                      "outcome": outcome, "ingested_at": ingested.isoformat(), "receipt_transaction_marker": receipt_marker.isoformat(),
                                      "first_observed_committed_at": observed.isoformat(), "visibility_latency_ms": round(latency_ms, 6)}
                            self.seen[key] = record
                            raw.write(json.dumps(record, separators=(",", ":")) + "\n")
                            self.last_observed_monotonic = time.perf_counter()
                        if rows:
                            # A wide overlap handles concurrent transaction-start
                            # markers. A final full scan below catches any older
                            # slow commit rather than losing it at a high-water mark.
                            newest = max(r[6] for r in rows)
                            cutoff = max(cutoff, newest - timedelta(seconds=30))
                    self.poll_ms.append((time.perf_counter() - began) * 1000)
                    if len(self.seen) == self.expected:
                        self.drained.set()
                    self.stop.wait(0.10)
                # Final complete receipt read validates the overlap assumption.
                for tenant_id, source, external, event, ingested, outcome, receipt_marker, observed in conn.execute("""SELECT e.tenant_id::text,e.source_id::text,e.source_event_id,e.event_id::text,e.ingested_at,r.outcome,r.committed_at,clock_timestamp()
                    FROM orderflow.processing_receipts r JOIN orderflow.inbox_events e USING(tenant_id,event_id)"""):
                    key = (tenant_id, source, external)
                    if key not in self.seen:
                        record = {"tenant_id": tenant_id, "source_id": source, "source_event_id": external, "event_id": event, "outcome": outcome,
                                  "ingested_at": ingested.isoformat(), "receipt_transaction_marker": receipt_marker.isoformat(),
                                  "first_observed_committed_at": observed.isoformat(), "visibility_latency_ms": round((observed - ingested).total_seconds() * 1000, 6), "final_scan_only": True}
                        self.seen[key] = record
                        raw.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception as error:
            self.error = type(error).__name__
            self.ready.set()


async def submit(base_url, fixture, data, output, concurrency):
    latencies, errors, accepted_ids = [], [], {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30, connect=5, pool=10)
    with gzip.open(output, "wt", encoding="utf-8") as raw:
        async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout, trust_env=False) as client:
            for phase_number, rows in enumerate(data["phases"]):
                queue = asyncio.Queue()
                for row in rows:
                    queue.put_nowait(row)
                async def consumer():
                    while True:
                        try:
                            row = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        started = time.perf_counter()
                        result = {"phase": phase_number, "category": row["category"], "tenant_id": row["envelope"]["tenant_id"],
                                  "source_id": row["source_id"], "source_event_id": row["envelope"]["source_event_id"]}
                        try:
                            response = await client.post(f"/v1/sources/{row['source_id']}/events", json=row["envelope"], headers={"X-Source-Key": fixture["source_keys"][row["source_id"]]})
                            result["http_status"] = response.status_code
                            payload = response.json()
                            if response.status_code != 202 or payload.get("duplicate") is not (row["category"] == "http_replay"):
                                result["error_code"] = payload.get("code", "unexpected_acceptance")
                                errors.append(result.copy())
                            else:
                                result.update(event_id=payload["event_id"], duplicate=payload["duplicate"], status=payload["status"])
                                key = identity(row)
                                if key in accepted_ids and accepted_ids[key] != payload["event_id"]:
                                    result["error_code"] = "identity_changed"
                                    errors.append(result.copy())
                                accepted_ids[key] = payload["event_id"]
                        except Exception as error:
                            result["error_code"] = type(error).__name__
                            errors.append(result.copy())
                        result["acceptance_latency_ms"] = round((time.perf_counter() - started) * 1000, 6)
                        latencies.append(result["acceptance_latency_ms"])
                        raw.write(json.dumps(result, separators=(",", ":")) + "\n")
                        queue.task_done()
                await asyncio.gather(*(consumer() for _ in range(concurrency)))
                print(json.dumps({"phase": phase_number, "http_attempts_completed": len(latencies), "errors": len(errors)}), flush=True)
    return latencies, errors, accepted_ids


def reconcile(fixture, data, observer):
    """Full-set observer plus pure Python expected state; no app imports."""
    checks = []
    def check(name, ok):
        checks.append({"name": name, "passed": bool(ok)})
    with psycopg.connect(**fixture["kwargs"], dbname=fixture["name"]) as conn:
        observed_inbox = conn.execute("SELECT tenant_id::text,source_id::text,source_event_id,status,payload_sha256,envelope_sha256,attempt_count FROM orderflow.inbox_events").fetchall()
        expected_ids = set(data["identities"])
        check("exact distinct inbox identity set", {r[:3] for r in observed_inbox} == expected_ids and len(observed_inbox) == len(expected_ids))
        correct_hashes = True
        for tenant, source, external, state, payload_hash, envelope_hash, attempts in observed_inbox:
            row = data["identities"].get((tenant, source, external))
            if row is None:
                correct_hashes = False
                continue
            body = row["envelope"]
            expected_payload = hashlib.sha256(json.dumps(body["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            canonical = {**body, "effective_at": body["effective_at"].replace("Z", ".000000Z"), "occurred_at": body["occurred_at"].replace("Z", ".000000Z")}
            expected_envelope = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            correct_hashes &= payload_hash == expected_payload and envelope_hash == expected_envelope and state == ("quarantined" if row["category"] == "quarantined" else "completed")
        check("all canonical hashes and terminal inbox states", correct_hashes)
        check("zero processing retries or lost attempts", all(row[6] == 1 for row in observed_inbox))
        revisions = conn.execute("""SELECT r.tenant_id::text,r.entity_type,r.entity_id,r.entity_version,r.source_id::text,e.source_event_id,r.effective_at,r.payload
            FROM orderflow.entity_revisions r JOIN orderflow.inbox_events e USING(tenant_id,event_id)""").fetchall()
        keys = {r[:4] for r in revisions}
        check("exact immutable revision key set", keys == set(data["revisions"]) and len(keys) == len(revisions))
        check("all revision payloads times and original event lineage", all((r[4] == data["revisions"][r[:4]]["source_id"] and r[5] == data["revisions"][r[:4]]["envelope"]["source_event_id"] and utc_string(r[6]) == data["revisions"][r[:4]]["envelope"]["effective_at"] and r[7] == data["revisions"][r[:4]]["envelope"]["payload"]) for r in revisions if r[:4] in data["revisions"]))
        current = conn.execute("SELECT tenant_id::text,entity_type,entity_id,entity_version,source_id::text,payload FROM orderflow.current_entities").fetchall()
        check("exact current entity keys", {r[:3] for r in current} == set(data["current"]) and len(current) == len(data["current"]))
        check("all latest versions payloads and authoritative sources", all(r[3] == data["current"][r[:3]]["envelope"]["entity_version"] and r[4] == data["current"][r[:3]]["source_id"] and r[5] == data["current"][r[:3]]["envelope"]["payload"] for r in current if r[:3] in data["current"]))
        receipt_rows = conn.execute("SELECT e.tenant_id::text,e.source_id::text,e.source_event_id,r.outcome,r.reason_code FROM orderflow.processing_receipts r JOIN orderflow.inbox_events e USING(tenant_id,event_id)").fetchall()
        check("exact receipt identity set", {r[:3] for r in receipt_rows} == expected_ids and len(receipt_rows) == len(expected_ids))
        outcomes_ok = True
        for row in receipt_rows:
            expected = data["identities"].get(row[:3])
            if expected is None:
                outcomes_ok = False
            elif expected["category"] == "quarantined":
                outcomes_ok &= row[3:] == ("quarantined", "tenant_currency_mismatch")
            elif expected["category"] == "duplicate_revision":
                outcomes_ok &= row[3:] == ("duplicate_revision", None)
            else:
                outcomes_ok &= row[3] in {"applied_current", "history_only"} and row[4] is None
        check("all receipt outcomes independently allowed", outcomes_ok)
        partition_rows = conn.execute("SELECT tenant_id::text,source_id::text,partition_key,committed_count FROM orderflow.processing_partitions").fetchall()
        check("exact partition checkpoint counts", {r[:3]: r[3] for r in partition_rows} == dict(data["partitions"]))
        bad_pointer = conn.execute("""SELECT count(*) FROM orderflow.processing_partitions p LEFT JOIN orderflow.inbox_events e
            ON(e.tenant_id=p.tenant_id AND e.source_id=p.source_id AND e.partition_key=p.partition_key AND e.event_id=p.last_committed_event_id)
            LEFT JOIN orderflow.processing_receipts r ON(r.tenant_id=e.tenant_id AND r.event_id=e.event_id)
            WHERE p.committed_count>0 AND (e.event_id IS NULL OR r.event_id IS NULL)""").fetchone()[0]
        check("all checkpoint pointers have scoped terminal receipts", bad_pointer == 0)
        quarantine_count = conn.execute("SELECT count(*) FROM orderflow.quarantines").fetchone()[0]
        check("exact business quarantine count", quarantine_count == data["mix"]["business_quarantines"])
        actual_intervals = conn.execute("""WITH chosen AS(SELECT DISTINCT ON(tenant_id,entity_id,effective_at) tenant_id,entity_id,effective_at,entity_version
            FROM orderflow.entity_revisions WHERE entity_type='product' ORDER BY tenant_id,entity_id,effective_at,entity_version DESC)
            SELECT tenant_id::text,entity_id,entity_version,effective_at,lead(effective_at) OVER(PARTITION BY tenant_id,entity_id ORDER BY effective_at) FROM chosen""").fetchall()
        grouped = {}
        for (tenant, kind, entity, version), row in data["revisions"].items():
            if kind == "product":
                times = grouped.setdefault((tenant, entity), {})
                effective = row["envelope"]["effective_at"]
                times[effective] = max(times.get(effective, 0), version)
        expected_intervals = set()
        for (tenant, entity), versions in grouped.items():
            times = sorted(versions)
            for index, begin in enumerate(times):
                expected_intervals.add((tenant, entity, versions[begin], begin, times[index + 1] if index + 1 < len(times) else None))
        actual_set = {(t, e, v, utc_string(start), utc_string(end) if end else None) for t, e, v, start, end in actual_intervals}
        check("SQL-derived history equals independent Python intervals", actual_set == expected_intervals and len(actual_intervals) == len(actual_set))
        expected_totals, actual_totals = {}, {}
        for tenant in TENANTS:
            expected_totals[tenant] = sum(row["envelope"]["payload"]["total_minor"] for (t, kind, _), row in data["current"].items() if t == tenant and kind == "order")
            actual_totals[tenant] = int(conn.execute("SELECT coalesce(sum((payload->>'total_minor')::numeric),0) FROM orderflow.current_entities WHERE tenant_id=%s AND entity_type='order'", (tenant,)).fetchone()[0])
        check("exact latest-order totals by tenant", actual_totals == expected_totals)
        check("observer saw every committed identity", set(observer.seen) == expected_ids)
        check("no negative visibility latency or missed-observer records", all(r["visibility_latency_ms"] >= 0 and not r.get("final_scan_only") for r in observer.seen.values()))
    return {"passed": all(row["passed"] for row in checks), "checks": checks,
            "observed_counts": {"inbox": len(observed_inbox), "revisions": len(revisions), "current": len(current), "receipts": len(receipt_rows), "quarantines": quarantine_count, "partitions": len(partition_rows)}, "latest_order_totals_minor": actual_totals}


def trial(data, workers, repetition, folder, manifest, concurrency, deadline):
    folder.mkdir(parents=True, exist_ok=False)
    harness = harness_manifest()
    report = {"workers": workers, "repetition": repetition, "source": manifest, "harness_sha256": harness, "passed": False, "generated_at": datetime.now(timezone.utc).isoformat(), "request_concurrency": concurrency}
    processes, files, fixture, observer = [], [], None, None
    try:
        with scratch_database("benchmark") as fixture:
            report["database"] = fixture["name"]
            with psycopg.connect(**fixture["kwargs"], dbname=fixture["name"]) as conn:
                version, connections, buffers = conn.execute("SELECT version(),current_setting('max_connections'),current_setting('shared_buffers')").fetchone()
                report["postgresql"] = {"version": version, "max_connections": connections, "shared_buffers": buffers}
            report["measurement_configuration"] = {"requests_per_minute": fixture["env"]["ODF_REQUESTS_PER_MINUTE"], "read_only_warmup_seconds": 0.4, "pool_size_per_process": 8, "max_pool_overflow": 8}
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            def launch(command, name):
                file = (folder / name).open("wb")
                files.append(file)
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=file, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                processes.append(process)
                return process
            api = launch([sys.executable, "-m", "uvicorn", "orderflow.api:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "warning"], "api.log")
            with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
                for _ in range(100):
                    if api.poll() is not None:
                        raise RuntimeError("Benchmark API exited before readiness")
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Benchmark API readiness timed out")
                tokens = {alias: client.post("/v1/dev/session", json={"identity_id": alias}).json()["access_token"] for alias in ("alpha-operator", "beta-operator")}
            for number in range(workers):
                launch([sys.executable, "-m", "orderflow.worker", "--tenant", TENANTS[0], "--tenant", TENANTS[1]], f"worker-{number + 1}.log")
            observer = VisibilityObserver(fixture, folder / "terminal-visibility.jsonl.gz", data["scale"])
            observer.thread.start()
            if not observer.ready.wait(10) or observer.error:
                raise RuntimeError("Independent visibility observer failed to start")
            # Read-only warmup: worker connections/health are ready; no hidden
            # materialization events contaminate the expected fixture counts.
            time.sleep(0.4)
            start = time.perf_counter()
            async def bounded_submission():
                return await asyncio.wait_for(submit(base_url, fixture, data, folder / "http-attempts.jsonl.gz", concurrency), timeout=deadline)
            latencies, errors, accepted_ids = asyncio.run(bounded_submission())
            acceptance_finish = time.perf_counter()
            report.update(http_attempts=len(latencies), http_errors=errors, distinct_accepted_ids=len(accepted_ids),
                          acceptance_seconds=round(acceptance_finish-start, 6))
            if errors:
                raise RuntimeError("HTTP acceptance errors were preserved; inspect raw attempt evidence before retrying")
            while not observer.drained.wait(0.1):
                if observer.error:
                    raise RuntimeError("Independent visibility observer failed")
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("A benchmark API/worker process exited")
                if time.perf_counter() - start > deadline:
                    raise TimeoutError("The benchmark did not drain before its explicit deadline")
            finish = max(acceptance_finish, observer.last_observed_monotonic or acceptance_finish)
            observer.stop.set()
            observer.thread.join(timeout=30)
            if observer.thread.is_alive() or observer.error:
                raise RuntimeError("Independent observer did not finish cleanly")
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("An API or worker exited unexpectedly before cleanup")
            for process in processes[1:]:
                stop_owned(process)
            reconciliation = reconcile(fixture, data, observer)
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                for index, alias in enumerate(("alpha-operator", "beta-operator")):
                    headers = {"Authorization": "Bearer " + tokens[alias]}
                    overview = client.get("/v1/overview", headers=headers)
                    expected_count = sum(key[0] == TENANTS[index] for key in data["identities"])
                    ok = overview.status_code == 200 and overview.json()["counts"]["accepted"] == expected_count and overview.json()["order_totals"]["current_amount_minor"] == reconciliation["latest_order_totals_minor"][TENANTS[index]]
                    reconciliation["checks"].append({"name": alias + " exact API count and latest total", "passed": ok})
                other = next(record for record in observer.seen.values() if record["tenant_id"] == TENANTS[1])
                denied = client.get("/v1/events/" + other["event_id"], headers={"Authorization": "Bearer " + tokens["alpha-operator"]})
                reconciliation["checks"].append({"name": "existing other-tenant event API returns404", "passed": denied.status_code == 404})
            reconciliation["passed"] = all(row["passed"] for row in reconciliation["checks"])
            visibility = [r["visibility_latency_ms"] for r in observer.seen.values()]
            visibility_groups = {}
            for outcome in sorted({r["outcome"] for r in observer.seen.values()}):
                values = [r["visibility_latency_ms"] for r in observer.seen.values() if r["outcome"] == outcome]
                visibility_groups[outcome] = {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            report.update(elapsed_seconds=round(finish - start, 6), acceptance_seconds=round(acceptance_finish - start, 6),
                          terminal_receipts_per_second=data["scale"] / (finish - start),
                          successful_materializations_per_second=(data["scale"] - data["mix"]["business_quarantines"] - data["mix"]["duplicate_revision_events"]) / (finish - start),
                          http_attempts=len(latencies), http_errors=errors, distinct_accepted_ids=len(accepted_ids),
                          acceptance_latency_ms={"n": len(latencies), "p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
                          observed_terminal_visibility_latency_ms={"n": len(visibility), "p50": percentile(visibility, 0.5), "p95": percentile(visibility, 0.95), "max": max(visibility)},
                          visibility_latency_by_outcome_ms=visibility_groups,
                          observer={"poll_wait_ms": 100, "max_query_cycle_ms": max(observer.poll_ms, default=0), "read_scope": "Privileged independent observer; adds measurement overhead to both worker configurations."},
                          reconciliation=reconciliation)
            if source_manifest() != manifest:
                raise RuntimeError("Core source or migrations changed during the benchmark")
            if harness_manifest() != harness:
                raise RuntimeError("Benchmark harness, seed metadata or lockfile changed during the trial")
            report["passed"] = not errors and len(accepted_ids) == data["scale"] and reconciliation["passed"]
    except Exception as error:
        report["failure_type"] = type(error).__name__
        # Only known runner diagnostics are emitted; never stringify DB errors.
        if isinstance(error, (RuntimeError, TimeoutError)):
            report["failure"] = str(error)
    finally:
        if observer:
            observer.stop.set()
            observer.thread.join(timeout=10)
        for process in reversed(processes):
            stop_owned(process)
        for file in files:
            file.close()
        report["owned_database_removed"] = fixture is not None and fixture["removed"]
        report["raw_artifacts"] = [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha(p), "bytes": p.stat().st_size} for p in sorted(folder.iterdir()) if p.is_file()]
        (folder / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, choices=(10000, 100000), default=10000)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--workers", type=int, nargs="+", choices=(1, 4), default=[1, 4])
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--deadline-seconds", type=int, default=3600)
    parser.add_argument("--label", default="native")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 64 or not args.label.replace("-", "").replace("_", "").isalnum():
        parser.error("Concurrency must be1–64 and label must be alphanumeric/dash/underscore")
    manifest = source_manifest()
    parent = ROOT / "artifacts/benchmarks"
    parent.mkdir(parents=True, exist_ok=True)
    # Larger scale is gated on a complete, successful six-run smaller series.
    if args.scale == 100000:
        gate_path = parent / f"{args.label}-10000-summary.json"
        gate = json.loads(gate_path.read_text())
        if not gate.get("complete") or not gate.get("passed") or gate["source"] != manifest:
            raise RuntimeError("100k requires all six10k runs to pass on the same frozen core source")
    data = dataset(args.scale, args.seed)
    raw_root = parent / "raw" / f"{args.label}-{args.scale}-seed{args.seed}-{manifest['sha256'][:12]}"
    raw_root.mkdir(parents=True, exist_ok=True)
    input_path = raw_root / "input.jsonl.gz"
    ledger_path = raw_root / "truth-ledger.jsonl.gz"
    if not input_path.exists():
        packed_json(input_path, ({"phase": n, **row} for n, rows in enumerate(data["phases"]) for row in rows))
        packed_json(ledger_path, (row for _, row in sorted(data["identities"].items())))
    summary_path = parent / f"{args.label}-{args.scale}-summary.json"
    summary = {"schema": "orderflow-core-benchmark-v1", "label": args.label, "generated_at": datetime.now(timezone.utc).isoformat(),
               "source": manifest, "seed": args.seed, "mix": data["mix"],
               "environment": environment_metadata(), "harness_sha256": harness_manifest(),
               "inputs": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha(p), "bytes": p.stat().st_size} for p in (input_path, ledger_path)],
               "scope": "Real loopback uvicorn HTTP acceptance, PostgreSQL durable inbox and independently launched partition workers; pipeline/dbt/hosted capacity excluded.",
               "latency_definition": "Stored receipt committed_at is a transaction marker, not exact commit time. Terminal latency uses first independently observed committed visibility minus stored ingested_at on the PostgreSQL server clock; it is an upper-bound observation including polling/query overhead.",
               "required_trials": [{"workers": w, "repetition": r} for w in (1, 4) for r in (1, 2, 3)], "runs": []}
    if args.resume and summary_path.exists():
        existing = json.loads(summary_path.read_text())
        if existing["source"] != manifest or existing["seed"] != args.seed or existing["inputs"] != summary["inputs"] or existing.get("harness_sha256") != summary["harness_sha256"]:
            raise RuntimeError("Refusing to combine changed core, harness, seed or inputs with an existing series")
        summary["runs"] = existing["runs"]
    elif summary_path.exists():
        raise RuntimeError("Existing evidence preserved; use --resume for the identical series or a new --label")
    def save():
        completed = {(r["workers"], r["repetition"]) for r in summary["runs"] if r.get("passed")}
        summary["complete"] = completed == {(w, r) for w in (1, 4) for r in (1, 2, 3)}
        summary["passed"] = bool(summary["runs"]) and all(r.get("passed") and r.get("owned_database_removed") for r in summary["runs"])
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    save()
    for workers in args.workers:
        for repetition in range(1, args.repetitions + 1):
            prior = next((r for r in summary["runs"] if r["workers"] == workers and r["repetition"] == repetition), None)
            if prior:
                if not prior.get("passed"):
                    raise RuntimeError("Failed trial preserved; investigate before starting a new labeled series")
                continue
            print(json.dumps({"starting": {"workers": workers, "repetition": repetition, "distinct_events": args.scale}}), flush=True)
            report = trial(data, workers, repetition, raw_root / f"workers-{workers}-rep-{repetition}", manifest, args.concurrency, args.deadline_seconds)
            summary["runs"].append(report)
            save()
            print(json.dumps({"workers": workers, "repetition": repetition, "passed": report["passed"], "elapsed_seconds": report.get("elapsed_seconds"), "terminal_receipts_per_second": report.get("terminal_receipts_per_second"), "failure_type": report.get("failure_type")}), flush=True)
            if not report["passed"]:
                raise SystemExit(1)
    print(json.dumps({"summary": str(summary_path), "passed": summary["passed"], "complete": summary["complete"], "runs": len(summary["runs"])}))


if __name__ == "__main__":
    main()
