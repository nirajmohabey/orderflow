"""Continuously poll bounded PostgreSQL partition transactions."""
import argparse
import json
import logging
import signal
import threading
import time

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .config import Settings
from .db import primitive, tenant_transaction

log = logging.getLogger("orderflow.worker")


def _one(conn, sql, params):
    row = conn.execute(text(sql), params).mappings().one_or_none()
    return dict(row) if row is not None else None


def _finish(conn, event, outcome, reason=None):
    params = {"tenant": str(event["tenant_id"]), "event": str(event["event_id"]),
              "source": str(event["source_id"]), "partition": event["partition_key"],
              "outcome": outcome, "reason": reason,
              "status": "quarantined" if outcome == "quarantined" else "completed"}
    if outcome == "quarantined":
        conn.execute(text("INSERT INTO orderflow.quarantines(tenant_id,event_id,reason_code,redacted_details) VALUES(:tenant,:event,:reason,'{}'::jsonb)"), params)
    conn.execute(text("INSERT INTO orderflow.processing_receipts(tenant_id,event_id,outcome,reason_code) VALUES(:tenant,:event,:outcome,:reason)"), params)
    conn.execute(text("UPDATE orderflow.inbox_events SET status=:status,attempt_count=attempt_count+1,last_error_code=:reason WHERE tenant_id=:tenant AND event_id=:event"), params)
    conn.execute(text("UPDATE orderflow.processing_partitions SET committed_count=committed_count+1,last_committed_event_id=:event,last_commit_at=now() WHERE tenant_id=:tenant AND source_id=:source AND partition_key=:partition"), params)
    return {"event_id": params["event"], "status": params["status"], "outcome": outcome, "reason_code": reason}


def _materialize(conn, event, fault):
    params = {"tenant": str(event["tenant_id"]), "type": event["entity_type"], "entity": event["entity_id"],
              "version": event["entity_version"], "source": str(event["source_id"]), "event": str(event["event_id"]),
              "effective": event["effective_at"], "hash": event["payload_sha256"],
              "payload": json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":"))}
    tenant_currency = conn.execute(text("SELECT currency FROM orderflow.tenants WHERE tenant_id=:tenant"), params).scalar_one()
    authority = conn.execute(text("SELECT source_id FROM orderflow.source_authorities WHERE tenant_id=:tenant AND entity_type=:type"), params).scalar_one_or_none()
    if authority is None or str(authority) != params["source"]:
        return _finish(conn, event, "quarantined", "source_not_authoritative")
    # The first durable authoritative envelope reserves the version's business
    # meaning even when it later quarantines. Otherwise a corrected payload
    # could reuse a failed version instead of declaring a new source version.
    first = _one(conn, """SELECT payload_sha256,payload,effective_at FROM orderflow.inbox_events
        WHERE tenant_id=:tenant AND source_id=:source AND entity_type=:type AND entity_id=:entity
        AND entity_version=:version ORDER BY inbox_sequence LIMIT 1""", params)
    if first is None:
        raise RuntimeError("The accepted source version has no durable envelope")
    if (first["payload_sha256"] != event["payload_sha256"] or first["payload"] != event["payload"]
            or first["effective_at"] != event["effective_at"]):
        return _finish(conn, event, "quarantined", "entity_version_conflict")
    if event["payload"].get("currency") != tenant_currency:
        return _finish(conn, event, "quarantined", "tenant_currency_mismatch")
    existing = _one(conn, "SELECT payload_sha256,payload,effective_at FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type=:type AND entity_id=:entity AND entity_version=:version", params)
    if existing:
        if existing["payload_sha256"] != event["payload_sha256"] or existing["payload"] != event["payload"] or existing["effective_at"] != event["effective_at"]:
            return _finish(conn, event, "quarantined", "entity_version_conflict")
        return _finish(conn, event, "duplicate_revision")
    lower = conn.execute(text("SELECT effective_at FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type=:type AND entity_id=:entity AND entity_version<:version ORDER BY entity_version DESC LIMIT 1"), params).scalar_one_or_none()
    upper = conn.execute(text("SELECT effective_at FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type=:type AND entity_id=:entity AND entity_version>:version ORDER BY entity_version LIMIT 1"), params).scalar_one_or_none()
    if (lower is not None and lower > event["effective_at"]) or (upper is not None and upper < event["effective_at"]):
        return _finish(conn, event, "quarantined", "effective_time_conflict")
    conn.execute(text("""INSERT INTO orderflow.entity_revisions(tenant_id,entity_type,entity_id,entity_version,source_id,event_id,effective_at,payload_sha256,payload)
        VALUES(:tenant,:type,:entity,:version,:source,:event,:effective,:hash,CAST(:payload AS jsonb))"""), params)
    if fault:
        fault("after_revision", primitive(dict(event)), conn)
    current = conn.execute(text("SELECT entity_version FROM orderflow.entity_current WHERE tenant_id=:tenant AND entity_type=:type AND entity_id=:entity"), params).scalar_one_or_none()
    if current is None:
        conn.execute(text("INSERT INTO orderflow.entity_current(tenant_id,entity_type,entity_id,entity_version) VALUES(:tenant,:type,:entity,:version)"), params)
        outcome = "applied_current"
    elif event["entity_version"] > current:
        conn.execute(text("UPDATE orderflow.entity_current SET entity_version=:version,updated_at=now() WHERE tenant_id=:tenant AND entity_type=:type AND entity_id=:entity"), params)
        outcome = "applied_current"
    else:
        outcome = "history_only"
    return _finish(conn, event, outcome)


def _schedule_failure(event, error_code):
    """A fresh transaction after rollback; never overwrites a winning worker."""
    params = {"tenant": str(event["tenant_id"]), "source": str(event["source_id"]),
              "partition": event["partition_key"], "event": str(event["event_id"])}
    with tenant_transaction(params["tenant"]) as conn:
        conn.execute(text("SELECT partition_key FROM orderflow.processing_partitions WHERE tenant_id=:tenant AND source_id=:source AND partition_key=:partition FOR UPDATE"), params).one()
        current = _one(conn, "SELECT * FROM orderflow.inbox_events WHERE tenant_id=:tenant AND event_id=:event FOR UPDATE", params)
        receipt = conn.execute(text("SELECT 1 FROM orderflow.processing_receipts WHERE tenant_id=:tenant AND event_id=:event"), params).scalar_one_or_none()
        if receipt or current["status"] not in {"pending", "retry_wait"}:
            return {"event_id": params["event"], "status": current["status"], "superseded": True}
        # Another failure scheduler may already have recorded this attempt.
        if current["attempt_count"] != event["attempt_count"]:
            return {"event_id": params["event"], "status": current["status"], "superseded": True}
        attempt = current["attempt_count"] + 1
        if attempt >= Settings.from_env().max_attempts:
            return _finish(conn, current, "quarantined", "retry_exhausted")
        params.update({"attempt": attempt, "delay": min(60, 2 ** (attempt - 1)), "error": error_code})
        conn.execute(text("UPDATE orderflow.inbox_events SET status='retry_wait',attempt_count=:attempt,available_at=now()+(:delay * interval '1 second'),last_error_code=:error WHERE tenant_id=:tenant AND event_id=:event"), params)
        return {"event_id": params["event"], "status": "retry_wait", "attempt_count": attempt, "reason_code": error_code}


def process_one(tenant_id, *, fault=None):
    event = None
    try:
        with tenant_transaction(tenant_id) as conn:
            partition = _one(conn, """WITH candidate_events AS MATERIALIZED (
                SELECT e.source_id,e.partition_key,e.available_at,e.inbox_sequence
                FROM orderflow.inbox_events e
                JOIN orderflow.sources s ON s.tenant_id=e.tenant_id AND s.source_id=e.source_id AND s.enabled
                WHERE e.tenant_id=:tenant AND e.status IN('pending','retry_wait') AND e.available_at<=now()
                ORDER BY e.available_at,e.inbox_sequence LIMIT 128)
                SELECT locked.tenant_id,locked.source_id,locked.partition_key
                FROM candidate_events candidate CROSS JOIN LATERAL (
                    SELECT p.tenant_id,p.source_id,p.partition_key FROM orderflow.processing_partitions p
                    WHERE p.tenant_id=:tenant AND p.source_id=candidate.source_id AND p.partition_key=candidate.partition_key
                    FOR UPDATE OF p SKIP LOCKED LIMIT 1
                ) locked LIMIT 1""", {"tenant": str(tenant_id)})
            if partition is None:
                return None
            params = {"tenant": str(tenant_id), "source": str(partition["source_id"]), "partition": partition["partition_key"]}
            event = _one(conn, """SELECT * FROM orderflow.inbox_events WHERE tenant_id=:tenant AND source_id=:source
                AND partition_key=:partition AND status IN('pending','retry_wait') AND available_at<=now()
                ORDER BY inbox_sequence FOR UPDATE LIMIT 1""", params)
            if event is None:
                return None
            receipt = conn.execute(text("SELECT 1 FROM orderflow.processing_receipts WHERE tenant_id=:tenant AND event_id=:event"), {"tenant": str(tenant_id), "event": str(event["event_id"])}).scalar_one_or_none()
            if receipt:
                raise RuntimeError("Nonterminal event has an unexpected receipt")
            if fault:
                fault("before_materialize", primitive(dict(event)), conn)
            result = _materialize(conn, event, fault)
            if fault:
                fault("before_commit", primitive(dict(event)), conn)
    except Exception as error:
        if event is None:
            raise
        code = "transient_database_error" if isinstance(error, DBAPIError) else "processing_error"
        log.warning("A processing attempt rolled back (%s)", code)
        return _schedule_failure(event, code)
    if fault:
        # Kept outside the retry handler: the transaction has already committed.
        fault("after_commit", primitive(dict(event)), None)
    return result


def drain(tenant_ids, *, max_events=10000, deadline_seconds=60):
    end = time.monotonic() + deadline_seconds
    processed = 0
    outcomes = {}
    while processed < max_events and time.monotonic() < end:
        progressed = False
        for tenant in tenant_ids:
            result = process_one(tenant)
            if result is not None:
                processed += 1
                progressed = True
                key = result.get("outcome", result["status"])
                outcomes[key] = outcomes.get(key, 0) + 1
            if processed >= max_events:
                break
        if not progressed:
            break
    return {"processed": processed, "outcomes": outcomes, "deadline_reached": time.monotonic() >= end, "limit_reached": processed >= max_events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from uuid import UUID
    tenants = tuple(str(UUID(v)) for v in args.tenant) or Settings.from_env().worker_tenants
    if not tenants:
        parser.error("Explicit worker tenant allowlist is required")
    if args.once:
        print(json.dumps(drain(tenants)))
        return
    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopping.set())
    while not stopping.is_set():
        progressed = False
        for tenant in tenants:
            if stopping.is_set():
                break
            try:
                progressed = process_one(tenant) is not None or progressed
            except Exception:
                log.error("Worker database attempt failed; retrying after bounded idle backoff")
                stopping.wait(1)
        if not progressed:
            stopping.wait(0.25)


if __name__ == "__main__":
    main()
