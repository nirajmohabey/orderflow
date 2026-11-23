"""Bounded real HTTP adapters and independent observed/reference CSV parsing."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import re
from uuid import UUID

import httpx
from sqlalchemy import text

from .db import tenant_transaction

MAX_HTTP_BYTES = 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 5000


class ConnectorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _json_request(client, method, url, **kwargs):
    try:
        headers={**kwargs.pop('headers',{}),'Accept-Encoding':'identity'}
        with client.stream(method, url, headers=headers, **kwargs) as response:
            if response.status_code != (202 if method == "POST" else 200):
                raise ConnectorError("http_acceptance_unconfirmed" if method == "POST" else "catalog_unavailable")
            if response.headers.get('Content-Encoding','').strip().lower() not in {'','identity'}:
                raise ConnectorError('http_content_encoding_unsupported')
            raw = bytearray()
            # Never invoke transparent decompression on an upstream response.
            # The content branch supports already-buffered test transports;
            # actual streaming HTTP is bounded in raw16KiB chunks.
            pieces=(response.content,) if response.is_stream_consumed else response.iter_raw(chunk_size=16384)
            for piece in pieces:
                if len(piece) > MAX_HTTP_BYTES - len(raw):
                    raise ConnectorError("http_response_too_large")
                raw.extend(piece)
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (httpx.HTTPError, UnicodeError, ValueError, RecursionError) as error:
        raise ConnectorError("http_response_invalid") from error


def _base_url(value):
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password or url.query or url.fragment:
        raise ConnectorError("invalid_http_base")
    return str(url).rstrip("/")


def post_event(client, api_base, source_id, source_key, envelope):
    value = _json_request(client, "POST", _base_url(api_base) + "/v1/sources/" + str(UUID(str(source_id))) + "/events",
                          headers={"X-Source-Key": source_key}, json=envelope)
    if (not isinstance(value, dict) or value.get("status") not in {"pending", "retry_wait", "completed", "quarantined"}
            or not isinstance(value.get("duplicate"), bool)):
        raise ConnectorError("invalid_acceptance_receipt")
    try:
        UUID(value["event_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConnectorError("invalid_acceptance_receipt") from error
    return value


def read_cursor(tenant_id, source_id, name="catalog"):
    with tenant_transaction(tenant_id, actor_subject="catalog-connector") as conn:
        row = conn.execute(text("""SELECT cursor_value,cursor_revision FROM orderflow.connector_cursors
            WHERE tenant_id=:t AND source_id=:s AND cursor_name=:n"""),
            {"t": str(tenant_id), "s": str(source_id), "n": name}).mappings().one_or_none()
    return (dict(row["cursor_value"]), row["cursor_revision"]) if row else ({}, 0)


def advance_cursor(tenant_id, source_id, old_revision, value, accepted_names, name="catalog"):
    with tenant_transaction(tenant_id, actor_subject="catalog-connector") as conn:
        # HTTP202 is the public durable contract; this local observer additionally
        # verifies every page identity before the fetch cursor can advance.
        count = conn.execute(text("""SELECT count(*) FROM orderflow.inbox_events WHERE tenant_id=:t AND source_id=:s
            AND source_event_id=ANY(CAST(:names AS text[]))"""),
            {"t": str(tenant_id), "s": str(source_id), "names": list(set(accepted_names))}).scalar_one()
        if count != len(set(accepted_names)):
            raise ConnectorError("page_acceptance_not_durable")
        parameters = {"t": str(tenant_id), "s": str(source_id), "n": name, "v": json.dumps(value), "r": old_revision}
        if old_revision == 0:
            row = conn.execute(text("""INSERT INTO orderflow.connector_cursors
                (tenant_id,source_id,cursor_name,cursor_value,cursor_revision) VALUES(:t,:s,:n,CAST(:v AS jsonb),1)
                ON CONFLICT DO NOTHING RETURNING cursor_revision"""), parameters).one_or_none()
        else:
            row = conn.execute(text("""UPDATE orderflow.connector_cursors SET cursor_value=CAST(:v AS jsonb),
                cursor_revision=cursor_revision+1,updated_at=now() WHERE tenant_id=:t AND source_id=:s
                AND cursor_name=:n AND cursor_revision=:r RETURNING cursor_revision"""), parameters).one_or_none()
        if row is None:
            raise ConnectorError("cursor_revision_conflict")
        return row[0]


def ingest_catalog(tenant_id, source_id, source_key, catalog_url, api_base, *, max_pages=1000, client=None, after_item=None, refresh=False):
    """Fetch no next page until every current-page POST has a committed202.

    after_item is an explicit local fault barrier, never an HTTP-selectable hook.
    """
    if not 1 <= max_pages <= 1000:
        raise ConnectorError("invalid_page_limit")
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    pages = accepted = duplicates = 0
    try:
        for _ in range(max_pages):
            cursor, revision = read_cursor(tenant_id, source_id)
            new_cycle = bool(cursor.get("completed") and refresh and pages == 0)
            if cursor.get("completed") and not new_cycle:
                return {"pages": pages, "accepted": accepted, "duplicates": duplicates, "completed": True}
            page = _json_request(client, "GET", _base_url(catalog_url) + "/catalog", params={"cursor": "" if new_cycle else cursor.get("next_cursor", "")})
            if (not isinstance(page, dict) or not isinstance(page.get("items"), list) or len(page["items"]) > 1000
                    or not re.fullmatch(r"[a-f0-9]{64}", page.get("dataset_sha256", ""))
                    or (page.get("next_cursor") is not None and not re.fullmatch(r"p[0-9]{1,8}", page["next_cursor"]))):
                raise ConnectorError("invalid_catalog_page")
            if not new_cycle and cursor.get("dataset_sha256") not in {None, page["dataset_sha256"]}:
                raise ConnectorError("catalog_changed_during_pagination")
            if not new_cycle and page.get("next_cursor") is not None and page["next_cursor"] == cursor.get("next_cursor"):
                raise ConnectorError("catalog_cursor_did_not_advance")
            names = []
            for envelope in page["items"]:
                if not isinstance(envelope, dict) or str(envelope.get("tenant_id")) != str(tenant_id):
                    raise ConnectorError("catalog_tenant_mismatch")
                result = post_event(client, api_base, source_id, source_key, envelope)
                names.append(envelope["source_event_id"])
                accepted += 1
                duplicates += int(result["duplicate"])
                if after_item:
                    after_item(accepted, result)
            value = {"next_cursor": page.get("next_cursor"), "completed": page.get("next_cursor") is None,
                     "dataset_sha256": page["dataset_sha256"]}
            advance_cursor(tenant_id, source_id, revision, value, names)
            pages += 1
            if value["completed"]:
                return {"pages": pages, "accepted": accepted, "duplicates": duplicates, "completed": True}
        raise ConnectorError("page_limit_reached")
    finally:
        if own_client:
            client.close()


def ingest_order_events(api_base, source_id, source_key, records, *, client=None):
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
    results = []
    try:
        for index, envelope in enumerate(records):
            if index >= 100000:
                raise ConnectorError("event_batch_limit")
            results.append(post_event(client, api_base, source_id, source_key, envelope))
        return {"accepted": len(results), "duplicates": sum(item["duplicate"] for item in results)}
    finally:
        if own_client:
            client.close()


def catalog_app(records, page_size=2):
    """A bounded paginated HTTP simulator; launch it separately for real I/O tests."""
    from fastapi import FastAPI, HTTPException
    if not 1 <= page_size <= 1000 or len(records) > 100000:
        raise ValueError("Invalid catalog fixture size")
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    app = FastAPI(title="OrderFlow synthetic catalog")

    @app.get("/catalog")
    def catalog(cursor: str = ""):
        if cursor and not re.fullmatch(r"p[0-9]{1,8}", cursor):
            raise HTTPException(400, "Invalid cursor")
        offset = int(cursor[1:]) if cursor else 0
        if offset > len(records):
            raise HTTPException(400, "Invalid cursor")
        end = min(offset + page_size, len(records))
        return {"items": records[offset:end], "next_cursor": "p" + str(end) if end < len(records) else None,
                "dataset_sha256": digest}
    return app


def parse_inventory(raw: bytes, *, reference=False):
    if not raw or len(raw) > MAX_CSV_BYTES:
        raise ConnectorError("inventory_file_size")
    identity = "reference_id" if reference else "snapshot_id"
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        if reader.fieldnames != ["tenant_id", identity, "as_of", "sku", "quantity"]:
            raise ConnectorError("inventory_columns")
        rows, metadata = {}, None
        for index, record in enumerate(reader):
            if index >= MAX_CSV_ROWS or None in record or any(value is None for value in record.values()):
                raise ConnectorError("inventory_row_shape")
            tenant = str(UUID(record["tenant_id"]))
            as_of = datetime.fromisoformat(record["as_of"].replace("Z", "+00:00"))
            if as_of.tzinfo is None:
                raise ConnectorError("inventory_timezone")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record[identity]):
                raise ConnectorError("inventory_identity")
            current = {"tenant_id": tenant, identity: record[identity], "as_of": as_of.astimezone(timezone.utc).isoformat()}
            if metadata is not None and metadata != current:
                raise ConnectorError("inventory_mixed_snapshot")
            metadata = current
            sku = record["sku"]
            if not 1 <= len(sku) <= 100 or any(ord(char) < 32 for char in sku) or sku in rows:
                raise ConnectorError("inventory_duplicate_or_invalid_sku")
            if not re.fullmatch(r"0|[1-9][0-9]{0,12}", record["quantity"]):
                raise ConnectorError("inventory_quantity")
            rows[sku] = int(record["quantity"])
        if metadata is None:
            raise ConnectorError("inventory_empty")
        return {**metadata, "quantities": rows, "sha256": hashlib.sha256(raw).hexdigest()}
    except (UnicodeError, csv.Error, ValueError, TypeError) as error:
        raise ConnectorError("inventory_invalid") from error
