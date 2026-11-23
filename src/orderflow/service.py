"""Bounded authenticated transactions and tenant-scoped operations reads."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from uuid import UUID

from sqlalchemy import text

from .auth import Principal
from .config import Settings
from .db import primitive, tenant_transaction
from .schemas import canonical_envelope, canonical_payload, partition_key

STATES = {"pending", "retry_wait", "completed", "quarantined"}


class DomainError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def _one(conn, sql, params):
    row = conn.execute(text(sql), params).mappings().one_or_none()
    return dict(row) if row is not None else None


def _rows(conn, sql, params):
    return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _member(conn, principal, operator=False):
    member = _one(conn, "SELECT * FROM orderflow.resolve_member(:issuer,:subject,CAST(:tenant AS uuid))", {"issuer": Settings.from_env().jwt_issuer, "subject": principal.subject, "tenant": principal.tenant_id})
    if member is None:
        raise DomainError(403, "membership_required", "An active membership is required.")
    if operator and member["role"] != "operator":
        raise DomainError(403, "operator_required", "An operator role is required.")
    return Principal(str(member["tenant_id"]), member["subject"], member["role"])


def _missing():
    raise DomainError(404, "not_found", "The requested resource was not found.")


def rate_limit(tenant_id, subject, action):
    settings = Settings.from_env()
    key = hashlib.sha256((str(subject) + ":" + action).encode()).hexdigest()
    bucket = int(time.time() // 60)
    with tenant_transaction(tenant_id) as conn:
        used = conn.execute(text("""INSERT INTO orderflow.request_budgets(tenant_id,budget_key,minute_bucket,used)
            VALUES(:tenant,:key,:bucket,1) ON CONFLICT(tenant_id,budget_key,minute_bucket)
            DO UPDATE SET used=orderflow.request_budgets.used+1 RETURNING used"""), {"tenant": str(tenant_id), "key": key, "bucket": bucket}).scalar_one()
        if used > settings.requests_per_minute:
            raise DomainError(429, "rate_limited", "The request budget was exceeded; retry later.")
        if used % 256 == 0:
            conn.execute(text("DELETE FROM orderflow.request_budgets WHERE tenant_id=:tenant AND minute_bucket<:old"), {"tenant": str(tenant_id), "old": bucket - 2})


def accept_event(source, envelope):
    if str(envelope.tenant_id) != source.tenant_id:
        raise DomainError(403, "source_scope_mismatch", "The source credential cannot accept this tenant.")
    envelope_hash = hashlib.sha256(canonical_envelope(envelope)).hexdigest()
    payload_hash = hashlib.sha256(canonical_payload(envelope)).hexdigest()
    params = {"tenant": source.tenant_id, "source": source.source_id, "external": envelope.source_event_id}
    with tenant_transaction(source.tenant_id, source.source_id, "source") as conn:
        enabled = _one(conn, "SELECT source_id FROM orderflow.sources WHERE tenant_id=:tenant AND source_id=:source AND enabled", params)
        if enabled is None:
            raise DomainError(403, "source_disabled", "The source is unavailable.")
        existing = _one(conn, "SELECT event_id,status,envelope_sha256 FROM orderflow.inbox_events WHERE tenant_id=:tenant AND source_id=:source AND source_event_id=:external", params)
        if existing:
            if not hmac.compare_digest(existing["envelope_sha256"], envelope_hash):
                raise DomainError(409, "event_identity_conflict", "This source event identity already has different semantic content.")
            return primitive({"event_id": existing["event_id"], "duplicate": True, "status": existing["status"]})
        if envelope.partition_key != partition_key(envelope.entity_type, envelope.entity_id):
            raise DomainError(422, "invalid_partition", "The partition does not match the stable entity partition.")
        params.update({"partition": envelope.partition_key, "type": envelope.entity_type, "entity": envelope.entity_id,
                       "version": envelope.entity_version, "effective": envelope.effective_at, "occurred": envelope.occurred_at,
                       "payload": canonical_payload(envelope).decode(), "payload_hash": payload_hash, "envelope_hash": envelope_hash})
        conn.execute(text("INSERT INTO orderflow.processing_partitions(tenant_id,source_id,partition_key) VALUES(:tenant,:source,:partition) ON CONFLICT DO NOTHING"), params)
        # Serialize sequence assignment and durable first meaning for this
        # authoritative entity with other acceptances and its materializer.
        conn.execute(text("SELECT partition_key FROM orderflow.processing_partitions WHERE tenant_id=:tenant AND source_id=:source AND partition_key=:partition FOR UPDATE"), params).one()
        inserted = _one(conn, """INSERT INTO orderflow.inbox_events(tenant_id,source_id,source_event_id,partition_key,schema_version,
            entity_type,entity_id,entity_version,effective_at,occurred_at,payload,payload_sha256,envelope_sha256)
            VALUES(:tenant,:source,:external,:partition,'1',:type,:entity,:version,:effective,:occurred,CAST(:payload AS jsonb),:payload_hash,:envelope_hash)
            ON CONFLICT(tenant_id,source_id,source_event_id) DO NOTHING RETURNING event_id,status""", params)
        if inserted is not None:
            result = {"event_id": inserted["event_id"], "duplicate": False, "status": inserted["status"]}
        else:
            existing = _one(conn, "SELECT event_id,status,envelope_sha256 FROM orderflow.inbox_events WHERE tenant_id=:tenant AND source_id=:source AND source_event_id=:external", params)
            if not hmac.compare_digest(existing["envelope_sha256"], envelope_hash):
                raise DomainError(409, "event_identity_conflict", "This source event identity already has different semantic content.")
            result = {"event_id": existing["event_id"], "duplicate": True, "status": existing["status"]}
    # The context manager committed before the acceptance response is returned.
    return primitive(result)


EVENT_COLUMNS = """e.event_id,e.source_id,e.source_event_id,e.entity_type,e.entity_id,e.entity_version,
    e.status,e.attempt_count,e.ingested_at,e.effective_at,e.occurred_at,e.available_at,
    e.envelope_sha256,e.payload_sha256,r.outcome,coalesce(r.reason_code,e.last_error_code) AS reason_code,r.committed_at"""


def get_event(principal, event_id):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        row = _one(conn, f"SELECT {EVENT_COLUMNS} FROM orderflow.inbox_events e LEFT JOIN orderflow.processing_receipts r USING(tenant_id,event_id) WHERE e.tenant_id=:tenant AND e.event_id=:id", {"tenant": principal.tenant_id, "id": str(event_id)})
        if row is None:
            _missing()
        return primitive(row)


def retry_event(principal, event_id, reason):
    from .schemas import RetryRequest
    reason = RetryRequest(reason=reason).reason
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        p = _member(conn, principal, operator=True)
        event = _one(conn, "SELECT event_id,status FROM orderflow.inbox_events WHERE tenant_id=:tenant AND event_id=:id FOR UPDATE", {"tenant": p.tenant_id, "id": str(event_id)})
        if event is None:
            _missing()
        receipt = conn.execute(text("SELECT 1 FROM orderflow.processing_receipts WHERE tenant_id=:tenant AND event_id=:id"), {"tenant": p.tenant_id, "id": str(event_id)}).scalar_one_or_none()
        if event["status"] != "retry_wait" or receipt:
            raise DomainError(409, "event_not_retryable", "Only a nonterminal retry-wait event can be expedited.")
        conn.execute(text("UPDATE orderflow.inbox_events SET status='pending',available_at=now() WHERE tenant_id=:tenant AND event_id=:id"), {"tenant": p.tenant_id, "id": str(event_id)})
        conn.execute(text("INSERT INTO orderflow.operator_audit(tenant_id,actor_subject,action,target_type,target_id,reason) VALUES(:tenant,:actor,'expedite_retry','event',:id,:reason)"), {"tenant": p.tenant_id, "actor": p.subject, "id": str(event_id), "reason": reason})
        return {"event_id": str(event_id), "status": "pending"}


def tenant_info(principal):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        row = _one(conn, "SELECT tenant_id,name,currency FROM orderflow.tenants WHERE tenant_id=:tenant", {"tenant": principal.tenant_id})
        if row is None:
            _missing()
        return primitive(row)


SOURCE_QUERY = """SELECT s.source_id,s.name,s.connector_type,s.enabled,
    (SELECT count(*) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id) AS accepted_count,
    (SELECT count(*) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id AND e.status IN('pending','retry_wait')) AS pending_count,
    (SELECT count(*) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id AND e.status='retry_wait') AS retry_wait_count,
    (SELECT count(*) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id AND e.status='completed') AS completed_count,
    (SELECT count(*) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id AND e.status='quarantined') AS quarantine_count,
    (SELECT min(ingested_at) FROM orderflow.inbox_events e WHERE e.tenant_id=s.tenant_id AND e.source_id=s.source_id AND e.status IN('pending','retry_wait')) AS oldest_pending_at,
    (SELECT max(last_commit_at) FROM orderflow.processing_partitions p WHERE p.tenant_id=s.tenant_id AND p.source_id=s.source_id) AS last_commit_at,
    (SELECT max(interval_end) FROM orderflow.ingestion_runs r WHERE r.tenant_id=s.tenant_id AND r.source_id=s.source_id AND r.status='succeeded') AS last_successful_interval_end,
    coalesce((SELECT jsonb_agg(a.entity_type ORDER BY a.entity_type) FROM orderflow.source_authorities a WHERE a.tenant_id=s.tenant_id AND a.source_id=s.source_id),'[]'::jsonb) AS authoritative_entity_types
    FROM orderflow.sources s WHERE s.tenant_id=:tenant"""


def list_sources(principal):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        return {"items": primitive(_rows(conn, SOURCE_QUERY + " ORDER BY s.name,s.source_id", {"tenant": principal.tenant_id}))}


def source_status(principal, source_id):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        row = _one(conn, SOURCE_QUERY + " AND s.source_id=:source", {"tenant": principal.tenant_id, "source": str(source_id)})
        if row is None:
            _missing()
        return primitive(row)


def get_run(principal, run_id):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        row = _one(conn, "SELECT run_id,source_id,status,interval_start,interval_end,started_at,finished_at,failure_code FROM orderflow.ingestion_runs WHERE tenant_id=:tenant AND run_id=:id", {"tenant": principal.tenant_id, "id": str(run_id)})
        if row is None:
            _missing()
        row["checks"] = _rows(conn, "SELECT check_name AS name,status,observed,expected FROM orderflow.quality_results WHERE tenant_id=:tenant AND run_id=:id ORDER BY check_name,check_id", {"tenant": principal.tenant_id, "id": str(run_id)})
        return primitive(row)


def list_runs(principal, limit=20):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        return {"items": primitive(_rows(conn, "SELECT run_id,source_id,status,interval_start,interval_end,started_at,finished_at,failure_code FROM orderflow.ingestion_runs WHERE tenant_id=:tenant ORDER BY interval_end DESC,run_id DESC LIMIT :limit", {"tenant": principal.tenant_id, "limit": min(max(limit, 1), 100)}))}


def overview(principal):
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        params = {"tenant": principal.tenant_id}
        tenant = _one(conn, "SELECT tenant_id,name,currency FROM orderflow.tenants WHERE tenant_id=:tenant", params)
        if tenant is None:
            _missing()
        counts = _one(conn, """SELECT count(*) AS accepted,count(*) FILTER(WHERE status='pending') AS pending,
            count(*) FILTER(WHERE status='retry_wait') AS retry_wait,count(*) FILTER(WHERE status='completed') AS completed,
            count(*) FILTER(WHERE status='quarantined') AS quarantined FROM orderflow.inbox_events WHERE tenant_id=:tenant""", params)
        counts.update(_one(conn, """SELECT (SELECT count(*) FROM orderflow.entity_revisions WHERE tenant_id=:tenant) AS revisions,
            count(*) FILTER(WHERE entity_type='product') AS products,count(*) FILTER(WHERE entity_type='order') AS orders
            FROM orderflow.entity_current WHERE tenant_id=:tenant""", params))
        totals = _one(conn, """SELECT coalesce(sum((payload->>'total_minor')::numeric),0) AS current_amount_minor,
            coalesce(sum((payload->>'total_minor')::numeric) FILTER(WHERE payload->>'status'='paid'),0) AS paid_amount_minor
            FROM orderflow.current_entities WHERE tenant_id=:tenant AND entity_type='order'""", params)
        totals["currency"] = tenant["currency"]
        freshness = _one(conn, """SELECT (SELECT min(ingested_at) FROM orderflow.inbox_events WHERE tenant_id=:tenant AND status IN('pending','retry_wait')) AS oldest_pending_at,
            (SELECT max(last_commit_at) FROM orderflow.processing_partitions WHERE tenant_id=:tenant) AS last_commit_at,
            (SELECT max(interval_end) FROM orderflow.ingestion_runs WHERE tenant_id=:tenant AND status='succeeded') AS last_successful_interval_end""", params)
        sources = _rows(conn, SOURCE_QUERY + " ORDER BY s.name,s.source_id", params)
        runs = _rows(conn, "SELECT run_id,source_id,status,interval_start,interval_end,started_at,finished_at,failure_code FROM orderflow.ingestion_runs WHERE tenant_id=:tenant ORDER BY interval_end DESC,run_id DESC LIMIT 10", params)
        now = conn.execute(text("SELECT now()")).scalar_one()
        return primitive({"tenant": tenant, "observed_at": now, "counts": counts, "order_totals": totals, "freshness": freshness, "sources": sources, "recent_runs": runs})


def _scope(principal, kind, filters=None):
    return {"tenant": principal.tenant_id, "kind": kind, "filter": hashlib.sha256(json.dumps(filters or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]}


def _cursor(scope, position):
    raw = json.dumps({**scope, "position": position, "exp": int(time.time()) + 3600}, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(Settings.from_env().auth_secret.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _position(scope, cursor):
    if not cursor:
        return None
    try:
        if len(cursor) > 512 or not cursor.isascii():
            raise ValueError
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        raw, signature = decoded[:-32], decoded[-32:]
        if not hmac.compare_digest(signature, hmac.new(Settings.from_env().auth_secret.encode(), raw, hashlib.sha256).digest()):
            raise ValueError
        result = json.loads(raw)
        if any(result.get(key) != value for key, value in scope.items()) or result["exp"] < time.time() or not isinstance(result["position"], dict):
            raise ValueError
        return result["position"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise DomainError(422, "invalid_cursor", "The cursor is invalid, expired or belongs to another query.") from None


def _time_position(position):
    try:
        return {"at": datetime.fromisoformat(position["at"].replace("Z", "+00:00")), "id": str(UUID(position["id"]))}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DomainError(422, "invalid_cursor", "The cursor position is invalid.") from None


def _limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError(422, "invalid_page_size", "Page size must be between 1 and 100.")


def list_quarantines(principal, limit=25, cursor=None):
    _limit(limit)
    scope = _scope(principal, "quarantines")
    position = _position(scope, cursor)
    params = {"tenant": principal.tenant_id, "limit": limit + 1}
    condition = ""
    if position:
        params.update(_time_position(position))
        condition = " AND (created_at,quarantine_id)<(:at,CAST(:id AS uuid))"
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        items = _rows(conn, "SELECT quarantine_id,event_id,reason_code,created_at,redacted_details FROM orderflow.quarantines WHERE tenant_id=:tenant" + condition + " ORDER BY created_at DESC,quarantine_id DESC LIMIT :limit", params)
    more = len(items) > limit
    items = primitive(items[:limit])
    next_cursor = _cursor(scope, {"at": items[-1]["created_at"], "id": items[-1]["quarantine_id"]}) if more else None
    return {"items": items, "next_cursor": next_cursor}


def list_events(principal, limit=25, cursor=None, status=None, source_id=None):
    _limit(limit)
    if status is not None and status not in STATES:
        raise DomainError(422, "invalid_event_filter", "The event status filter is invalid.")
    source_id = str(UUID(str(source_id))) if source_id is not None else None
    scope = _scope(principal, "events", {"status": status, "source_id": source_id})
    position = _position(scope, cursor)
    params = {"tenant": principal.tenant_id, "limit": limit + 1, "status": status, "source": source_id}
    condition = ""
    if position:
        params.update(_time_position(position))
        condition = " AND (e.ingested_at,e.event_id)<(:at,CAST(:id AS uuid))"
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        items = _rows(conn, f"SELECT {EVENT_COLUMNS} FROM orderflow.inbox_events e LEFT JOIN orderflow.processing_receipts r USING(tenant_id,event_id) WHERE e.tenant_id=:tenant AND (CAST(:status AS text) IS NULL OR e.status=:status) AND (CAST(:source AS uuid) IS NULL OR e.source_id=CAST(:source AS uuid))" + condition + " ORDER BY e.ingested_at DESC,e.event_id DESC LIMIT :limit", params)
    more = len(items) > limit
    items = primitive(items[:limit])
    return {"items": items, "next_cursor": _cursor(scope, {"at": items[-1]["ingested_at"], "id": items[-1]["event_id"]}) if more else None}


def list_current(principal, entity_type, limit=25, cursor=None):
    _limit(limit)
    if entity_type not in {"product", "order"}:
        raise DomainError(422, "invalid_entity_type", "Entity type must be product or order.")
    scope = _scope(principal, "current", {"type": entity_type})
    position = _position(scope, cursor)
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        after = None
        if position is not None:
            try:
                event_id = str(UUID(position["event_id"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                raise DomainError(422, "invalid_cursor", "The cursor position is invalid.") from None
            # A UUID into an immutable revision keeps even a 200-character
            # Unicode entity ID within the 512-character cursor budget. Its
            # revision survives later changes to the current pointer.
            after = conn.execute(text("SELECT entity_id FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type=:type AND event_id=:event"), {"tenant": principal.tenant_id, "type": entity_type, "event": event_id}).scalar_one_or_none()
            if after is None:
                raise DomainError(422, "invalid_cursor", "The cursor revision is unavailable.")
        items = _rows(conn, """SELECT entity_id,entity_version,source_id,event_id,effective_at,payload FROM orderflow.current_entities
            WHERE tenant_id=:tenant AND entity_type=:type AND (CAST(:after AS text) IS NULL OR entity_id>:after)
            ORDER BY entity_id LIMIT :limit""", {"tenant": principal.tenant_id, "type": entity_type, "after": after, "limit": limit + 1})
    more = len(items) > limit
    items = primitive(items[:limit])
    return {"items": items, "next_cursor": _cursor(scope, {"event_id": items[-1]["event_id"]}) if more else None}


def product_history(principal, entity_id):
    if not isinstance(entity_id, str) or not 1 <= len(entity_id) <= 200 or "\x00" in entity_id:
        raise DomainError(422, "invalid_entity_id", "The entity identifier is invalid.")
    params = {"tenant": principal.tenant_id, "entity": entity_id}
    with tenant_transaction(principal.tenant_id, principal.subject, principal.role) as conn:
        _member(conn, principal)
        # Bound a highly versioned entity rather than silently truncating history.
        count = conn.execute(text("SELECT count(*) FROM (SELECT 1 FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type='product' AND entity_id=:entity LIMIT 1001) versions"), params).scalar_one()
        if count == 0:
            _missing()
        if count > 1000:
            raise DomainError(422, "history_budget_exceeded", "This entity requires an offline history export.")
        raw = _rows(conn, "SELECT entity_version,effective_at,event_id,payload FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type='product' AND entity_id=:entity ORDER BY entity_version", params)
        intervals = _rows(conn, """WITH selected AS (
            SELECT DISTINCT ON(effective_at) entity_version,effective_at,payload
            FROM orderflow.entity_revisions WHERE tenant_id=:tenant AND entity_type='product' AND entity_id=:entity
            ORDER BY effective_at,entity_version DESC)
            SELECT entity_version,effective_at AS valid_from,lead(effective_at) OVER(ORDER BY effective_at) AS valid_to,payload
            FROM selected ORDER BY effective_at""", params)
        return primitive({"entity_id": entity_id, "raw_revisions": raw, "intervals": intervals})
