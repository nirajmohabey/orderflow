"""Finite snapshot → immutable exports → real dbt checks → last-good pointer."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from .connectors import ConnectorError, MAX_CSV_BYTES, parse_inventory
from .db import get_engine, primitive, tenant_transaction
from .objects import LocalObjectStore, ObjectError

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TESTS = {"order_reconciliation", "total_reconciliation", "expectation_row_count", "history_nonoverlap",
                  "history_highest_equal_time", "revision_key_unique", "fact_relationship"}
PREVIEW_LIMIT = 100


class PipelineError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return json.dumps(primitive(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PipelineError("utc_interval_required")
    return value.astimezone(timezone.utc)


def object_store():
    return LocalObjectStore(os.environ.get("ODF_OBJECT_ROOT", str(ROOT / ".runtime/objects")))


def start_run(tenant_id, source_id, start, end):
    start, end = timestamp(start), timestamp(end)
    if end <= start or end - start > timedelta(days=31):
        raise PipelineError("invalid_interval")
    run_id = str(uuid4())
    with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
        source = conn.execute(text("SELECT connector_type FROM orderflow.sources WHERE tenant_id=:t AND source_id=:s AND enabled"),
                              {"t": str(tenant_id), "s": str(source_id)}).scalar_one_or_none()
        if source is None:
            raise PipelineError("source_not_available")
        conn.execute(text("""INSERT INTO orderflow.ingestion_runs(tenant_id,run_id,source_id,status,interval_start,interval_end,started_at)
            VALUES(:t,:r,:s,'running',:start,:end,clock_timestamp())"""),
            {"t": str(tenant_id), "r": run_id, "s": str(source_id), "start": start, "end": end})
    return run_id


def _check(conn, tenant_id, run_id, name, status, observed=None, expected=None):
    conn.execute(text("""INSERT INTO orderflow.quality_results(tenant_id,run_id,check_name,status,observed,expected)
        VALUES(:t,:r,:name,:status,:observed,:expected)"""),
        {"t": str(tenant_id), "r": str(run_id), "name": name[:200], "status": status, "observed": observed, "expected": expected})


def fail_run(tenant_id, run_id, code):
    with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
        conn.execute(text("""UPDATE orderflow.ingestion_runs SET status='failed',finished_at=clock_timestamp(),failure_code=:code
            WHERE tenant_id=:t AND run_id=:r AND status IN ('queued','running')"""), {"t": str(tenant_id), "r": str(run_id), "code": code[:100]})
        conn.execute(text("""UPDATE orderflow.report_candidates SET status='failed' WHERE tenant_id=:t AND run_id=:r
            AND status IN ('staging','checked')"""), {"t": str(tenant_id), "r": str(run_id)})


def finish_source_run(tenant_id, run_id, accepted):
    with tenant_transaction(tenant_id, actor_subject='connector', role='pipeline') as conn:
        _check(conn, tenant_id, run_id, 'durable_http_acceptance', 'pass', accepted, accepted)
        conn.execute(text("""UPDATE orderflow.ingestion_runs SET status='succeeded',finished_at=clock_timestamp(),failure_code=NULL
            WHERE tenant_id=:t AND run_id=:r AND status='running'"""), {'t':str(tenant_id),'r':str(run_id)})


def _project_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "warehouse").rglob("*")):
        if path.is_file() and path.suffix in {".sql", ".yml", ".yaml", ".in"} and not any(x in {"target", "logs", "dbt_packages"} for x in path.parts):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def load_oracle(path, tenant_id):
    with Path(path).open('rb') as source:
        raw = source.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise PipelineError("oracle_too_large")
    data = json.loads(raw)
    entries = data.get("tenants", [data])
    matches = [row for row in entries if str(row.get("tenant_id")) == str(tenant_id)]
    if len(matches) != 1:
        raise PipelineError("oracle_tenant_missing")
    oracle = matches[0]
    orders = oracle.get("orders")
    if (not isinstance(orders, list) or len(orders) > 100000 or len({row["entity_id"] for row in orders}) != len(orders)
            or type(oracle.get("order_count")) is not int or type(oracle.get("order_total_minor")) is not int
            or oracle["order_count"] != len(orders) or oracle["order_total_minor"] != sum(row["total_minor"] for row in orders)):
        raise PipelineError("invalid_independent_oracle")
    for row in orders:
        if (type(row["entity_version"]) is not int or type(row["total_minor"]) is not int
                or row["entity_version"] < 1 or row["total_minor"] < 0 or not isinstance(row["entity_id"], str)):
            raise PipelineError("invalid_independent_oracle")
    return {"tenant_id": str(tenant_id), "orders": sorted(orders, key=lambda row: row["entity_id"]),
            "order_count": oracle["order_count"], "order_total_minor": oracle["order_total_minor"]}


def stage_daily(tenant_id, source_id, start, end, oracle_path, *, store=None, fault=None):
    tenant_id, source_id = str(UUID(str(tenant_id))), str(UUID(str(source_id)))
    start, end = timestamp(start), timestamp(end)
    if end-start != timedelta(days=1) or start.time() != datetime.min.time():
        raise PipelineError("fixed_daily_interval_required")
    oracle = load_oracle(oracle_path, tenant_id)
    store = store or object_store()
    run_id = start_run(tenant_id, source_id, start, end)
    try:
        with tempfile.TemporaryDirectory(prefix="orderflow-export-") as temporary:
            files = []
            with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
                p = {"t": tenant_id, "r": run_id, "end": end}
                conn.execute(text("INSERT INTO orderflow.report_locks(tenant_id,report_key) VALUES(:t,'daily') ON CONFLICT DO NOTHING"), p)
                conn.execute(text("SELECT 1 FROM orderflow.report_locks WHERE tenant_id=:t AND report_key='daily' FOR UPDATE"), p)
                currency = conn.execute(text("SELECT currency FROM orderflow.tenants WHERE tenant_id=:t"), p).scalar_one().strip()
                if any(row["currency"] != currency for row in oracle["orders"]):
                    raise PipelineError("oracle_currency_mismatch")
                conn.execute(text("""INSERT INTO orderflow.report_candidates(tenant_id,run_id,status,oracle_sha256,project_sha256)
                    VALUES(:t,:r,'staging',:oracle,:project)"""), {**p, "oracle": hashlib.sha256(encoded(oracle)).hexdigest(), "project": _project_hash()})
                conn.execute(text("""INSERT INTO orderflow.pipeline_expectations(tenant_id,run_id,order_total_minor,order_count,currency)
                    VALUES(:t,:r,:total,:count,:currency)"""), {**p, "total": oracle["order_total_minor"], "count": oracle["order_count"], "currency": currency})
                if oracle["orders"]:
                    conn.execute(text("""INSERT INTO orderflow.pipeline_expected_orders(tenant_id,run_id,entity_id,entity_version,total_minor,currency,status)
                        VALUES(:t,:r,:entity_id,:entity_version,:total_minor,:currency,:status)"""), [{**p, **row} for row in oracle["orders"]])
                # One INSERT SELECT is one PostgreSQL snapshot. Late materialized
                # versions already committed are included regardless of arrival day.
                conn.execute(text("""INSERT INTO orderflow.pipeline_snapshot_rows
                    (tenant_id,run_id,entity_type,entity_id,entity_version,source_id,event_id,effective_at,payload)
                    SELECT tenant_id,:r,entity_type,entity_id,entity_version,source_id,event_id,effective_at,payload
                    FROM orderflow.entity_revisions WHERE tenant_id=:t AND effective_at<:end"""), p)
                result = conn.execution_options(stream_results=True).execute(text("""SELECT tenant_id,entity_type,entity_id,entity_version,
                    source_id,event_id,effective_at,payload FROM orderflow.pipeline_snapshot_rows
                    WHERE tenant_id=:t AND run_id=:r ORDER BY entity_type,entity_id,entity_version"""), p)
                digest = hashlib.sha256(); count = 0; buffer = bytearray(); shard_rows = 0
                def flush():
                    nonlocal buffer, shard_rows
                    if not buffer: return
                    path = Path(temporary) / (str(len(files)) + ".jsonl")
                    path.write_bytes(buffer); files.append((path, shard_rows)); buffer = bytearray(); shard_rows = 0
                for row in result.mappings():
                    line = encoded(dict(row)) + b"\n"
                    if len(line) > 32768: raise PipelineError("snapshot_row_too_large")
                    if len(buffer) + len(line) > 1024 * 1024: flush()
                    buffer.extend(line); shard_rows += 1; count += 1; digest.update(line)
                result.close(); flush()
                conn.execution_options(stream_results=False)
                authorities = [dict(row) for row in conn.execute(text("SELECT entity_type,source_id FROM orderflow.source_authorities WHERE tenant_id=:t ORDER BY entity_type"), p).mappings()]
                conn.execute(text("UPDATE orderflow.report_candidates SET snapshot_sha256=:sha WHERE tenant_id=:t AND run_id=:r"), {**p, "sha": digest.hexdigest()})
            # No database locks are held while exporting objects or calling dbt.
            prefix = f"staging/{tenant_id}/{start.date().isoformat()}/{run_id}"
            objects = []
            for index, (path, rows) in enumerate(files):
                data = path.read_bytes(); digest_value = hashlib.sha256(data).hexdigest()
                objects.append(store.put_immutable(f"{prefix}/revisions-{index:05d}-{digest_value}.jsonl", data).as_dict() | {"rows": rows})
            if fault: fault("after_raw_export", run_id)
            manifest = {"schema_version": 1, "tenant_id": tenant_id, "run_id": run_id, "report_owner_source_id": source_id,
                        "participating_authorities": primitive(authorities), "interval_start": primitive(start), "interval_end": primitive(end),
                        "snapshot_sha256": digest.hexdigest(), "row_count": count, "objects": objects,
                        "oracle_sha256": hashlib.sha256(encoded(oracle)).hexdigest(), "storage": store.kind}
            reference = store.put_immutable(prefix + "/source_manifest.json", encoded(manifest)).as_dict()
            with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
                conn.execute(text("UPDATE orderflow.report_candidates SET manifest=CAST(:m AS jsonb) WHERE tenant_id=:t AND run_id=:r AND status='staging'"),
                             {"t": tenant_id, "r": run_id, "m": encoded({"source": manifest, "source_manifest_object": reference}).decode()})
            if fault: fault("after_staged_manifest", run_id)
            return {"tenant_id": tenant_id, "run_id": run_id}
    except Exception:
        fail_run(tenant_id, run_id, "snapshot_export_failed")
        raise


def _candidate(tenant_id, run_id):
    with tenant_transaction(tenant_id, role="pipeline") as conn:
        value = conn.execute(text("""SELECT c.*,r.source_id,r.interval_start,r.interval_end FROM orderflow.report_candidates c
            JOIN orderflow.ingestion_runs r USING(tenant_id,run_id) WHERE c.tenant_id=:t AND c.run_id=:r"""),
            {"t": str(tenant_id), "r": str(run_id)}).mappings().one_or_none()
    if value is None: raise PipelineError("candidate_not_found")
    return dict(value)


def _dbt_environment(tenant_id):
    value = os.environ.get("ODF_DBT_DATABASE_URL")
    if not value: raise PipelineError("dbt_database_not_configured")
    url = make_url(value)
    env = os.environ.copy()
    env.update({"ODF_DBT_HOST": url.host or "localhost", "ODF_DBT_PORT": str(url.port or 5432),
                "ODF_DBT_USER": url.username or "", "ODF_DBT_PASSWORD": url.password or "", "ODF_DBT_DBNAME": url.database or "",
                "ODF_DBT_SSLMODE": url.query.get("sslmode", "prefer"), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
                "PGOPTIONS": f"-c orderflow.tenant_id={UUID(str(tenant_id))} -c statement_timeout=30000 -c lock_timeout=5000"})
    return env


def _relation(name, run_id):
    if name not in {"order_facts", "product_history"}: raise ValueError("Unknown model")
    return '"orderflow_warehouse"."' + name + "_" + UUID(str(run_id)).hex + '"'


def _model_preview(tenant_id, run_id):
    with get_engine(os.environ["ODF_DBT_DATABASE_URL"]).begin() as conn:
        conn.execute(text("SELECT set_config('orderflow.tenant_id',:t,true)"), {"t": str(tenant_id)})
        order_table, history_table = _relation("order_facts", run_id), _relation("product_history", run_id)
        totals = dict(conn.execute(text(f"SELECT count(*) AS order_count,coalesce(sum(total_minor),0) AS order_total_minor FROM {order_table}")).mappings().one())
        totals["product_count"] = conn.execute(text(f"SELECT count(distinct entity_id) FROM {history_table}")).scalar_one()
        orders = [dict(row) for row in conn.execute(text(f"SELECT entity_id,entity_version,total_minor,currency,status FROM {order_table} ORDER BY entity_id LIMIT 101")).mappings()]
        history = [dict(row) for row in conn.execute(text(f"SELECT entity_id,entity_version,valid_from,valid_to,unit_price_minor,currency FROM {history_table} ORDER BY entity_id,valid_from LIMIT 101")).mappings()]
    return primitive({"summary": totals, "order_facts": orders[:100], "product_history": history[:100],
                      "preview_limit": 100, "preview_truncated": len(orders)>100 or len(history)>100})


def check_daily(tenant_id, run_id, *, store=None, test_corrupt_total=False, fault=None):
    store = store or object_store(); run_id = str(UUID(str(run_id))); tenant_id = str(UUID(str(tenant_id)))
    candidate = _candidate(tenant_id, run_id)
    if candidate["status"] == "checked": return {"tenant_id": tenant_id, "run_id": run_id}
    if candidate["status"] != "staging" or not candidate["manifest"]: raise PipelineError("candidate_not_staged")
    if test_corrupt_total and os.environ.get("ODF_APP_ENV") != "local": raise PipelineError("test_fault_disabled")
    try:
        original = candidate["manifest"]; source = original["source"]; ref = original["source_manifest_object"]
        if store.get(ref["key"], expected_sha256=ref["sha256"]) != encoded(source): raise PipelineError("source_manifest_mismatch")
        digest = hashlib.sha256(); count = 0
        for item in source["objects"]:
            raw = store.get(item["key"], expected_sha256=item["sha256"])
            rows = len(raw.splitlines())
            if rows != item["rows"]: raise PipelineError("source_row_count_mismatch")
            count += rows; digest.update(raw)
        if count != source["row_count"] or digest.hexdigest() != candidate["snapshot_sha256"]: raise PipelineError("snapshot_hash_mismatch")
        if candidate["project_sha256"] != _project_hash(): raise PipelineError("dbt_project_changed")
        work = ROOT / ".runtime/dbt" / run_id; work.mkdir(parents=True, exist_ok=True)
        executable = os.environ.get("ODF_DBT_EXECUTABLE", str(ROOT / ".venv-dbt" / ("Scripts/dbt.exe" if os.name=="nt" else "bin/dbt")))
        artifacts = []; results = None
        for phase in ["run", "test"]:
            command = [executable, "--log-path", str(work/"logs"), phase, "--project-dir", str(ROOT/"warehouse"),
                       "--profiles-dir", str(ROOT/"warehouse"), "--target-path", str(work), "--vars",
                       json.dumps({"tenant_id": tenant_id, "run_id": run_id, "test_corrupt_total": bool(test_corrupt_total)})]
            with (work/(phase+".log")).open("wb") as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, env=_dbt_environment(tenant_id), timeout=180)
            path = work/"run_results.json"
            results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            if results:
                artifacts.append(store.put_immutable(f"reports/{tenant_id}/{run_id}/dbt-{phase}-results.json", encoded(results)).as_dict())
            with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
                _check(conn, tenant_id, run_id, "dbt_"+phase+"_execution", "pass" if completed.returncode==0 else "error", completed.returncode, 0)
                if phase=="test" and results:
                    for item in results.get("results", []):
                        _check(conn, tenant_id, run_id, item["unique_id"], "pass" if item["status"]=="pass" else "fail", item.get("failures") or 0, 0)
            if completed.returncode or not results: raise PipelineError("dbt_"+phase+"_failed")
            if fault: fault("after_dbt_"+phase, run_id)
        names = {item["unique_id"].split(".")[2] for item in results["results"]}
        if not REQUIRED_TESTS <= names or any(item["status"] != "pass" for item in results["results"]): raise PipelineError("required_dbt_checks_missing")
        preview = _model_preview(tenant_id, run_id)
        with tenant_transaction(tenant_id, role="pipeline") as conn:
            currency = conn.execute(text("SELECT currency FROM orderflow.pipeline_expectations WHERE tenant_id=:t AND run_id=:r"), {"t":tenant_id,"r":run_id}).scalar_one().strip()
        preview["summary"].update(currency=currency, raw_revision_count=source["row_count"])
        manifest = {**original, "dbt_artifacts": artifacts, "required_tests": sorted(REQUIRED_TESTS), "dbt_tests_passed": len(results["results"]),
                    "test_corrupt_total": bool(test_corrupt_total), "project_sha256": candidate["project_sha256"]}
        reference = store.put_immutable(f"reports/{tenant_id}/{run_id}/report_manifest.json", encoded(manifest))
        payload = {**preview, "manifest": manifest, "manifest_sha256": reference.sha256, "storage": store.kind,
                   "artifact_availability": "local_only" if store.kind=="local" else "configured_object_store"}
        with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
            conn.execute(text("""UPDATE orderflow.report_candidates SET status='checked',manifest=CAST(:m AS jsonb),manifest_sha256=:sha,
                report_payload=CAST(:payload AS jsonb) WHERE tenant_id=:t AND run_id=:r AND status='staging'"""),
                {"t":tenant_id,"r":run_id,"m":encoded(manifest).decode(),"sha":reference.sha256,"payload":encoded(payload).decode()})
        return {"tenant_id":tenant_id,"run_id":run_id}
    except Exception:
        fail_run(tenant_id, run_id, "dbt_or_artifact_check_failed")
        raise


def _publish_daily(tenant_id, run_id, *, fault=None):
    tenant_id, run_id = str(UUID(str(tenant_id))), str(UUID(str(run_id)))
    if fault: fault("before_pointer_commit", run_id)
    with tenant_transaction(tenant_id, actor_subject="pipeline", role="pipeline") as conn:
        p = {"t":tenant_id,"r":run_id}
        conn.execute(text("SELECT 1 FROM orderflow.report_locks WHERE tenant_id=:t AND report_key='daily' FOR UPDATE"), p)
        candidate = conn.execute(text("""SELECT c.*,r.interval_start,r.interval_end FROM orderflow.report_candidates c
            JOIN orderflow.ingestion_runs r USING(tenant_id,run_id) WHERE c.tenant_id=:t AND c.run_id=:r FOR UPDATE OF c"""), p).mappings().one_or_none()
        if candidate is None: raise PipelineError("candidate_not_found")
        if candidate["status"] in {"published","superseded"}: return read_latest_report(tenant_id)
        if candidate["status"] != "checked": raise PipelineError("candidate_checks_not_passed")
        checks = conn.execute(text("SELECT status FROM orderflow.quality_results WHERE tenant_id=:t AND run_id=:r"), p).scalars().all()
        if not checks or any(value != "pass" for value in checks): raise PipelineError("candidate_checks_not_passed")
        prior = conn.execute(text("""SELECT p.*,c.snapshot_sha256,c.oracle_sha256,c.project_sha256 FROM orderflow.report_publications p
            JOIN orderflow.report_candidates c USING(tenant_id,run_id) WHERE p.tenant_id=:t AND p.report_key='daily'"""), p).mappings().one_or_none()
        identical = prior and prior["interval_end"]==candidate["interval_end"] and all(prior[key]==candidate[key] for key in ["snapshot_sha256","oracle_sha256","project_sha256"])
        older = prior and (candidate["interval_end"],candidate["generation"]) <= (prior["interval_end"],prior["generation"])
        if not identical and not older:
            conn.execute(text("""INSERT INTO orderflow.report_publications(tenant_id,report_key,run_id,generation,interval_start,interval_end,manifest_sha256)
                VALUES(:t,'daily',:r,:g,:start,:end,:sha) ON CONFLICT(tenant_id,report_key) DO UPDATE SET run_id=excluded.run_id,
                generation=excluded.generation,interval_start=excluded.interval_start,interval_end=excluded.interval_end,
                manifest_sha256=excluded.manifest_sha256,published_at=clock_timestamp()"""),
                {**p,"g":candidate["generation"],"start":candidate["interval_start"],"end":candidate["interval_end"],"sha":candidate["manifest_sha256"]})
        conn.execute(text("UPDATE orderflow.report_candidates SET status=:status WHERE tenant_id=:t AND run_id=:r"), {**p,"status":"superseded" if identical or older else "published"})
        conn.execute(text("UPDATE orderflow.ingestion_runs SET status='succeeded',finished_at=clock_timestamp(),failure_code=NULL WHERE tenant_id=:t AND run_id=:r"), p)
    if fault: fault("after_pointer_commit", run_id)
    return read_latest_report(tenant_id)


def publish_daily(tenant_id, run_id, *, store=None, fault=None):
    store=store or object_store()
    try:
        candidate=_candidate(tenant_id,run_id)
        if candidate['status']=='checked':
            raw=store.get(f"reports/{UUID(str(tenant_id))}/{UUID(str(run_id))}/report_manifest.json",
                          expected_sha256=candidate['manifest_sha256'])
            if raw!=encoded(candidate['manifest']): raise PipelineError('report_manifest_mismatch')
            manifest=candidate['manifest']
            for item in manifest['source']['objects']+manifest['dbt_artifacts']:
                store.get(item['key'],expected_sha256=item['sha256'])
        return _publish_daily(tenant_id,run_id,fault=fault)
    except Exception:
        fail_run(tenant_id,run_id,'report_publication_failed')
        raise


def run_daily(tenant_id, source_id, start, end, oracle_path, *, store=None, test_corrupt_total=False, fault=None):
    run = stage_daily(tenant_id, source_id, start, end, oracle_path, store=store, fault=fault)
    check_daily(tenant_id, run["run_id"], store=store, test_corrupt_total=test_corrupt_total, fault=fault)
    try:
        return publish_daily(tenant_id, run["run_id"], store=store, fault=fault)
    except Exception:
        fail_run(tenant_id, run["run_id"], "report_publication_failed")
        raise


def read_latest_report(tenant_id):
    with tenant_transaction(tenant_id, role="viewer") as conn:
        p={"t":str(tenant_id)}
        last = conn.execute(text("""SELECT p.run_id,p.interval_start,p.interval_end,p.published_at,r.source_id,c.report_payload
            FROM orderflow.report_publications p JOIN orderflow.report_candidates c USING(tenant_id,run_id)
            JOIN orderflow.ingestion_runs r USING(tenant_id,run_id) WHERE p.tenant_id=:t AND p.report_key='daily'"""), p).mappings().one_or_none()
        attempt=conn.execute(text("""SELECT r.run_id,r.status,r.interval_start,r.interval_end,r.started_at,r.finished_at,r.failure_code
            FROM orderflow.ingestion_runs r JOIN orderflow.report_candidates c USING(tenant_id,run_id)
            WHERE r.tenant_id=:t ORDER BY c.generation DESC LIMIT 1"""), p).mappings().one_or_none()
        source_failure=conn.execute(text("""SELECT r.run_id,r.status,r.interval_start,r.interval_end,r.started_at,r.finished_at,r.failure_code
            FROM orderflow.ingestion_runs r JOIN orderflow.sources s USING(tenant_id,source_id)
            LEFT JOIN orderflow.report_candidates c USING(tenant_id,run_id)
            WHERE r.tenant_id=:t AND s.connector_type<>'csv' AND c.run_id IS NULL AND r.status='failed'
            ORDER BY r.started_at DESC,r.run_id DESC LIMIT 1"""),p).mappings().one_or_none()
        if source_failure and (attempt is None or
                (source_failure['started_at'] or source_failure['finished_at'] or source_failure['interval_end'])>
                (attempt['started_at'] or attempt['finished_at'] or attempt['interval_end'])):
            attempt=source_failure
        latest=dict(attempt) if attempt else None
        if latest:
            latest["checks"]=[dict(row) for row in conn.execute(text("SELECT check_name AS name,status,observed,expected FROM orderflow.quality_results WHERE tenant_id=:t AND run_id=:r ORDER BY checked_at,check_id"), {**p,"r":latest["run_id"]}).mappings()]
        now=datetime.now(timezone.utc)
        failed_newer=latest and latest["status"]!='succeeded' and (last is None or latest["interval_end"]>=last["interval_end"])
        stale=last is None or bool(failed_newer) or now-last["interval_end"]>timedelta(days=1)
        good=({**dict(last["report_payload"]),**{key:last[key] for key in ["run_id","source_id","interval_start","interval_end","published_at"]}} if last else None)
    return primitive({"has_success":last is not None,"stale":stale,"observed_at":now,"last_good":good,"latest_attempt":latest})


def reconcile_inventory(tenant_id, source_id, observed_path, reference_path, *, store=None):
    store=store or object_store(); tenant_id=str(UUID(str(tenant_id)));source_id=str(UUID(str(source_id)))
    with Path(observed_path).open('rb') as source:observed_raw=source.read(MAX_CSV_BYTES+1)
    with Path(reference_path).open('rb') as source:reference_raw=source.read(MAX_CSV_BYTES+1)
    observed=parse_inventory(observed_raw);reference=parse_inventory(reference_raw,reference=True)
    if observed["tenant_id"]!=tenant_id or reference["tenant_id"]!=tenant_id or observed["as_of"]!=reference["as_of"]:
        raise ConnectorError("inventory_scope_or_asof_mismatch")
    with tenant_transaction(tenant_id,role="pipeline") as conn:
        kind=conn.execute(text('SELECT connector_type FROM orderflow.sources WHERE tenant_id=:t AND source_id=:s AND enabled'),
                          {'t':tenant_id,'s':source_id}).scalar_one_or_none()
        if kind!='csv':raise PipelineError('inventory_csv_source_required')
        existing=conn.execute(text("SELECT observed_sha256,reference_sha256,report FROM orderflow.inventory_snapshots WHERE tenant_id=:t AND source_id=:s AND snapshot_id=:snapshot"), {"t":tenant_id,"s":source_id,"snapshot":observed["snapshot_id"]}).mappings().one_or_none()
    if existing:
        if (existing["observed_sha256"],existing["reference_sha256"])!=(observed["sha256"],reference["sha256"]): raise PipelineError("inventory_identity_conflict")
        return existing["report"]
    run_id=start_run(tenant_id,source_id,timestamp(observed["as_of"]),timestamp(observed["as_of"])+timedelta(seconds=1))
    try:
        differences=[]
        for sku in sorted(set(observed["quantities"])|set(reference["quantities"])):
            a,b=observed["quantities"].get(sku,0),reference["quantities"].get(sku,0)
            if a!=b or sku not in observed["quantities"] or sku not in reference["quantities"]:
                differences.append({"sku":sku,"observed_quantity":a,"reference_quantity":b,"difference":a-b,
                                    "observed_present":sku in observed["quantities"],"reference_present":sku in reference["quantities"]})
        prefix=f"raw/{tenant_id}/{source_id}/{observed['snapshot_id']}"
        raw_objects=[store.put_immutable(f"{prefix}/{observed['sha256']}/observed.csv",observed_raw).as_dict(),
                     store.put_immutable(f"{prefix}/{reference['sha256']}/reference.csv",reference_raw).as_dict()]
        report={"run_id":run_id,"source_id":source_id,"snapshot_id":observed["snapshot_id"],"reference_id":reference["reference_id"],
                "as_of":observed["as_of"],"status":"fail" if differences else "pass","observed_sha256":observed["sha256"],"reference_sha256":reference["sha256"],
                "observed_total":sum(observed["quantities"].values()),"reference_total":sum(reference["quantities"].values()),
                "difference_total":sum(observed["quantities"].values())-sum(reference["quantities"].values()),"differences":differences,
                "storage":store.kind,"artifact_availability":"local_only" if store.kind=="local" else "configured_object_store"}
        # Run IDs identify attempts, not immutable snapshot content. Concurrent
        # identical attempts therefore publish identical object bytes.
        artifact=store.put_immutable(f"reports/{tenant_id}/{source_id}/{observed['snapshot_id']}/inventory_reconciliation.json",
                                    encoded({k:v for k,v in report.items() if k!='run_id'})).as_dict()
        manifest={"schema_version":1,"tenant_id":tenant_id,"source_id":source_id,"snapshot_id":observed["snapshot_id"],
                  "objects":raw_objects+[artifact],"observed_rows":len(observed["quantities"]),"reference_rows":len(reference["quantities"])}
        manifest_ref=store.put_immutable(f"reports/{tenant_id}/{source_id}/{observed['snapshot_id']}/manifest.json",encoded(manifest))
        report.update(manifest=manifest,manifest_sha256=manifest_ref.sha256)
        with tenant_transaction(tenant_id,actor_subject="inventory-connector",role="pipeline") as conn:
            conn.execute(text("""INSERT INTO orderflow.inventory_snapshots(tenant_id,source_id,snapshot_id,reference_id,run_id,as_of,
                observed_sha256,reference_sha256,manifest_sha256,status,report) VALUES(:t,:s,:snapshot,:reference,:r,:asof,:oh,:rh,:mh,:status,CAST(:report AS jsonb))"""),
                {"t":tenant_id,"s":source_id,"snapshot":observed["snapshot_id"],"reference":reference["reference_id"],"r":run_id,"asof":timestamp(observed["as_of"]),
                 "oh":observed["sha256"],"rh":reference["sha256"],"mh":manifest_ref.sha256,"status":report["status"],"report":encoded(report).decode()})
            _check(conn,tenant_id,run_id,"inventory_quantity_reconciliation",report["status"],report["observed_total"],report["reference_total"])
            conn.execute(text("UPDATE orderflow.ingestion_runs SET status=:status,finished_at=clock_timestamp(),failure_code=:failure WHERE tenant_id=:t AND run_id=:r"),
                         {"t":tenant_id,"r":run_id,"status":"failed" if differences else "succeeded","failure":"inventory_difference" if differences else None})
        return report
    except IntegrityError:
        fail_run(tenant_id,run_id,"inventory_duplicate_attempt")
        with tenant_transaction(tenant_id,role="pipeline") as conn:
            prior=conn.execute(text("SELECT observed_sha256,reference_sha256,report FROM orderflow.inventory_snapshots WHERE tenant_id=:t AND source_id=:s AND snapshot_id=:snapshot"),
                               {"t":tenant_id,"s":source_id,"snapshot":observed["snapshot_id"]}).mappings().one_or_none()
        if prior and (prior['observed_sha256'],prior['reference_sha256'])==(observed['sha256'],reference['sha256']):
            return prior['report']
        raise
    except Exception:
        fail_run(tenant_id,run_id,"inventory_report_failed")
        raise


def read_inventory_history(tenant_id, source_id=None, limit=20):
    limit=max(1,min(int(limit),20))
    with tenant_transaction(tenant_id,role="viewer") as conn:
        rows=conn.execute(text("""SELECT report FROM orderflow.inventory_snapshots WHERE tenant_id=:t
            AND (CAST(:s AS uuid) IS NULL OR source_id=CAST(:s AS uuid)) ORDER BY created_at DESC,source_id,snapshot_id LIMIT :limit"""),
            {"t":str(tenant_id),"s":str(UUID(str(source_id))) if source_id else None,"limit":limit+1}).mappings().all()
    return {"items":[row["report"] for row in rows[:limit]],"truncated":len(rows)>limit}


def read_inventory_latest(tenant_id, source_id=None):
    rows=read_inventory_history(tenant_id,source_id,limit=1)["items"]
    return {"has_snapshot":bool(rows),"latest":rows[0] if rows else None}
