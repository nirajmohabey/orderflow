"""Real-PostgreSQL acceptance; every test owns one newly created database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

import jwt
import psycopg
from psycopg import sql
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from orderflow.auth import SourcePrincipal
from orderflow.config import ROOT, Settings
from orderflow.db import dispose_engines, get_engine, migrate, seed, tenant_transaction
from orderflow.schemas import Envelope, canonical_envelope, parse_envelope, partition_key
from orderflow.worker import drain, process_one

TENANT = "11111111-1111-4111-8111-111111111111"
BETA = "22222222-2222-4222-8222-222222222222"
CATALOG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORDERS = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BETA_CATALOG = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


@pytest.fixture
def core(monkeypatch):
    Settings.from_env()
    runtime = make_url(os.environ.get("ODF_TEST_DATABASE_URL", ""))
    admin = make_url(os.environ.get("ODF_ADMIN_TEST_DATABASE_URL", ""))
    forbidden = {"dbname", "database", "host", "hostaddr", "port", "user", "password", "service", "servicefile"}
    if any(v.database != "orderflow_test" or v.drivername != "postgresql+psycopg" or forbidden.intersection(k.lower() for k in v.query) for v in (runtime, admin)):
        pytest.fail("Core integration tests require dedicated orderflow_test URLs without routing overrides")
    identities = []
    for url in (runtime, admin):
        with get_engine(url.render_as_string(hide_password=False)).connect() as conn:
            identities.append(tuple(conn.execute(text("SELECT current_database(),inet_server_addr()::text,inet_server_port()")).one()))
    if identities[0] != identities[1] or identities[0][0] != "orderflow_test":
        pytest.fail("Runtime/admin disposable database destinations differ")
    owned_name = "orderflow_core_test_" + uuid4().hex
    admin_connection = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_connection, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(owned_name)))
    runtime_url = runtime.set(database=owned_name).render_as_string(hide_password=False)
    admin_url = admin.set(database=owned_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ODF_DATABASE_URL", runtime_url)
    monkeypatch.setenv("ODF_ADMIN_DATABASE_URL", admin_url)
    monkeypatch.setenv("ODF_APP_ENV", "local")
    monkeypatch.setenv("ODF_DEMO_MODE", "true")
    keys = {s["source_id"]: secrets.token_urlsafe(36) for s in fixture("seed_metadata.json")["sources"]}
    issuer = Settings.from_env().jwt_issuer
    members = [{"issuer": issuer, "subject": prefix + "-" + role, "tenant_id": tenant, "role": role, "alias": prefix + "-" + role}
               for prefix, tenant in (("alpha", TENANT), ("beta", BETA)) for role in ("operator", "viewer")]
    try:
        migrate(admin_url)
        seed(admin_url, fixture("seed_metadata.json"), keys, members)
        from fastapi.testclient import TestClient
        from orderflow.api import app
        with TestClient(app) as client:
            session = client.post("/v1/dev/session", json={"identity_id": "alpha-operator"})
            assert session.status_code == 200, session.text
            token = session.json()["access_token"]
            viewer = client.post("/v1/dev/session", json={"identity_id": "alpha-viewer"}).json()["access_token"]
            beta = client.post("/v1/dev/session", json={"identity_id": "beta-operator"}).json()["access_token"]
            yield {"client": client, "keys": keys, "admin": admin_url, "auth": {"Authorization": "Bearer " + token},
                   "viewer": {"Authorization": "Bearer " + viewer}, "beta": {"Authorization": "Bearer " + beta}}
    finally:
        dispose_engines()
        # Only the exact fresh UUID name created by this fixture is removed.
        if not owned_name.startswith("orderflow_core_test_") or len(owned_name) != len("orderflow_core_test_") + 32:
            raise RuntimeError("Refusing an unrecognized test database cleanup")
        with psycopg.connect(admin_connection, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(owned_name)))


def product(name="event-1", entity="P1", version=1, price=1000, effective="2026-11-01T12:00:00Z", tenant=TENANT):
    return {"tenant_id": tenant, "source_event_id": name, "partition_key": partition_key("product", entity), "schema_version": "1",
            "entity_type": "product", "entity_id": entity, "entity_version": version, "effective_at": effective,
            "occurred_at": effective, "payload": {"sku": "SKU-1", "name": "Notebook", "unit_price_minor": price, "currency": "USD"}}


def post(core, body, source=CATALOG):
    return core["client"].post(f"/v1/sources/{source}/events", headers={"X-Source-Key": core["keys"][source]}, json=body)


def observe(core, statement):
    with get_engine(core["admin"]).connect() as conn:
        return conn.execute(text(statement)).all()


def test_readiness_and_real_role(core):
    assert core["client"].get("/health/ready").status_code == 200
    with get_engine().connect() as conn:
        role = conn.execute(text("SELECT current_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=current_user")).one()
        assert role[0] == "orderflow_app" and not any(role[1:])
        assert conn.execute(text("SELECT count(*) FROM orderflow.tenants")).scalar_one() == 0
        with pytest.raises(DBAPIError):
            conn.execute(text("SELECT * FROM orderflow.source_credentials"))


def test_canonical_golden_and_timezone_equivalence():
    body = product()
    expected = ('{"effective_at":"2026-11-01T12:00:00.000000Z","entity_id":"P1","entity_type":"product","entity_version":1,'
                '"occurred_at":"2026-11-01T12:00:00.000000Z","partition_key":"product:P1","payload":{"currency":"USD",'
                '"name":"Notebook","sku":"SKU-1","unit_price_minor":1000},"schema_version":"1","source_event_id":"event-1",'
                '"tenant_id":"11111111-1111-4111-8111-111111111111"}').encode()
    assert canonical_envelope(Envelope.model_validate(body)) == expected
    body["effective_at"] = "2026-11-01T13:00:00+01:00"
    body["occurred_at"] = "2026-11-01T12:00:00.000Z"
    assert canonical_envelope(Envelope.model_validate(body)) == expected


def test_partition_long_unicode_is_bounded_and_deterministic():
    name = "界" * 200
    key = partition_key("product", name)
    assert key == "product:sha256:" + hashlib.sha256(name.encode()).hexdigest()
    assert len(key.encode()) <= 128
    assert Envelope.model_validate(product(entity=name)).entity_id == name


def test_durable_identity_replay_conflict_and_current_status(core):
    body = product()
    first = post(core, body)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    replay = post(core, dict(reversed(list(body.items()))))
    assert replay.status_code == 202 and replay.json() == {**first.json(), "duplicate": True}
    changed = deepcopy(body)
    changed["payload"]["unit_price_minor"] += 1
    assert post(core, changed).status_code == 409
    changed_partition = {**body, "partition_key": "product:other"}
    assert post(core, changed_partition).status_code == 409
    assert post(core, {**changed_partition, "source_event_id": "new-invalid"}).status_code == 422
    assert process_one(TENANT)["outcome"] == "applied_current"
    replay = post(core, body)
    assert replay.json()["event_id"] == first.json()["event_id"] and replay.json()["status"] == "completed"
    assert observe(core, "SELECT count(*) FROM orderflow.inbox_events")[0][0] == 1
    assert observe(core, "SELECT sum(committed_count) FROM orderflow.processing_partitions")[0][0] == 1


def test_parallel_identity_and_workers_exact_effect(core):
    body = Envelope.model_validate(product())
    from orderflow.service import accept_event
    source = SourcePrincipal(TENANT, CATALOG)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: accept_event(source, body), range(24)))
    assert len({r["event_id"] for r in outcomes}) == 1
    assert sum(not r["duplicate"] for r in outcomes) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: process_one(TENANT), range(12)))
    assert observe(core, "SELECT count(*) FROM orderflow.processing_receipts")[0][0] == 1
    assert observe(core, "SELECT count(*) FROM orderflow.entity_revisions")[0][0] == 1
    assert observe(core, "SELECT sum(committed_count) FROM orderflow.processing_partitions")[0][0] == 1


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(entity_version=True), lambda b: b.update(entity_version=1.0),
    lambda b: b.update(entity_version=0), lambda b: b.update(entity_version=9223372036854775808),
    lambda b: b["payload"].update(unit_price_minor=True), lambda b: b["payload"].update(unit_price_minor=-1),
    lambda b: b["payload"].update(unit_price_minor=1000000000000001), lambda b: b["payload"].update(extra="x"),
    lambda b: b.update(extra="x"), lambda b: b.update(entity_id="\x00"),
    lambda b: b.update(effective_at="2026-11-01T12:00:00"), lambda b: b.update(effective_at="2026-11-01T12:00:00.1234567Z"),
])
def test_structural_rejection_no_inbox(core, mutation):
    body = product()
    mutation(body)
    response = post(core, body)
    assert response.status_code == 422
    assert set(response.json()) == {"code", "message", "request_id"}
    assert observe(core, "SELECT count(*) FROM orderflow.inbox_events")[0][0] == 0


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":"\xff"}', b'[' * 17 + b'0' + b']' * 17])
def test_unambiguous_json_before_inbox(core, raw):
    response = core["client"].post(f"/v1/sources/{CATALOG}/events", content=raw, headers={"Content-Type": "application/json", "X-Source-Key": core["keys"][CATALOG]})
    assert response.status_code == 422
    assert observe(core, "SELECT count(*) FROM orderflow.inbox_events")[0][0] == 0


def test_request_length_bound_and_error_headers(core):
    response = core["client"].post(f"/v1/sources/{CATALOG}/events", content=b"x" * 65537, headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and response.headers["X-Request-ID"]
    response = core["client"].post(f"/v1/sources/{CATALOG}/events", content=b"{}", headers={"Content-Type": "application/json", "Content-Length": "9" * 5000})
    assert response.status_code == 413


def test_hand_specified_fixture_projection_and_history(core):
    expected = fixture("expected_state.json")
    for filename in expected["sequence"]:
        for line in (ROOT / "fixtures" / filename).read_text().splitlines():
            row = json.loads(line)
            assert post(core, row["envelope"], row["source_id"]).status_code == 202
        drain([TENANT, BETA])
    assert observe(core, "SELECT count(*) FROM orderflow.inbox_events WHERE tenant_id='11111111-1111-4111-8111-111111111111'")[0][0] == 10
    assert observe(core, "SELECT count(*) FROM orderflow.entity_revisions WHERE tenant_id='11111111-1111-4111-8111-111111111111'")[0][0] == 8
    assert observe(core, "SELECT count(*) FROM orderflow.quarantines WHERE tenant_id='11111111-1111-4111-8111-111111111111'")[0][0] == 2
    overview = core["client"].get("/v1/overview", headers=core["auth"]).json()
    assert overview["order_totals"]["current_amount_minor"] == 8000
    assert overview["counts"]["products"] == overview["counts"]["orders"] == 2
    history = core["client"].get("/v1/products/P1/history", headers=core["auth"]).json()
    assert [r["entity_version"] for r in history["raw_revisions"]] == [1, 2, 3]
    assert [(r["entity_version"], r["valid_from"], r["valid_to"]) for r in history["intervals"]] == [
        (1, "2026-11-01T12:00:00Z", "2026-11-02T12:00:00Z"), (2, "2026-11-02T12:00:00Z", "2026-11-03T12:00:00Z"), (3, "2026-11-03T12:00:00Z", None)]
    p2 = core["client"].get("/v1/products/P2/history", headers=core["auth"]).json()
    assert len(p2["raw_revisions"]) == 2 and [r["entity_version"] for r in p2["intervals"]] == [2]


def test_tenant_role_credentials_and_signed_claims(core):
    accepted = post(core, product()).json()
    event_id = accepted["event_id"]
    assert core["client"].get(f"/v1/events/{event_id}", headers=core["beta"]).status_code == 404
    assert core["client"].get(f"/v1/sources/{CATALOG}/status", headers=core["beta"]).status_code == 404
    assert post(core, product(tenant=BETA)).status_code == 403
    assert core["client"].post(f"/v1/sources/{ORDERS}/events", json=product(), headers={"X-Source-Key": core["keys"][CATALOG]}).status_code == 401
    settings = Settings.from_env()
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"iss": settings.jwt_issuer, "aud": settings.jwt_audience, "sub": "alpha-viewer", "iat": now, "exp": now + timedelta(minutes=5), "role": "operator", "tenant_id": BETA}, settings.auth_secret, algorithm="HS256")
    session = core["client"].get("/v1/session", headers={"Authorization": "Bearer " + forged}).json()
    assert session["principal"]["role"] == "viewer" and session["principal"]["tenant_id"] == TENANT
    assert core["client"].get("/v1/session", headers={"Authorization": "Bearer " + forged, "X-Tenant-ID": BETA}).status_code == 403
    assert core["client"].post(f"/v1/events/{event_id}/retry", headers=core["viewer"], json={"reason": "Reviewed retry"}).status_code == 403


def test_late_neighbor_conflict_and_duplicate_revision(core):
    assert post(core, product("v1", version=1)).status_code == 202
    assert post(core, product("v3", version=3, effective="2026-11-03T12:00:00Z")).status_code == 202
    drain([TENANT])
    middle = product("bad-middle", version=2, effective="2026-11-04T12:00:00Z")
    result = post(core, middle).json()
    assert process_one(TENANT)["reason_code"] == "effective_time_conflict"
    assert core["client"].get(f"/v1/events/{result['event_id']}", headers=core["auth"]).json()["status"] == "quarantined"
    post(core, product("same-revision", version=1))
    assert process_one(TENANT)["outcome"] == "duplicate_revision"
    assert observe(core, "SELECT count(*) FROM orderflow.entity_revisions")[0][0] == 2
    assert observe(core, "SELECT sum(committed_count) FROM orderflow.processing_partitions")[0][0] == 4


def test_source_authority_and_currency_business_quarantine(core):
    assert post(core, product("wrong-source"), ORDERS).status_code == 202
    assert process_one(TENANT)["reason_code"] == "source_not_authoritative"
    body = product("wrong-currency", entity="P2")
    body["payload"]["currency"] = "EUR"
    assert post(core, body).status_code == 202
    assert process_one(TENANT)["reason_code"] == "tenant_currency_mismatch"
    assert observe(core, "SELECT count(*) FROM orderflow.entity_revisions")[0][0] == 0


def test_failure_rolls_back_then_retry_audit_and_terminal_guard(core):
    event_id = post(core, product()).json()["event_id"]
    def fail(stage, event, conn):
        if stage == "after_revision":
            raise RuntimeError("synthetic local failure")
    assert process_one(TENANT, fault=fail)["status"] == "retry_wait"
    assert observe(core, "SELECT count(*) FROM orderflow.entity_revisions")[0][0] == 0
    assert observe(core, "SELECT count(*) FROM orderflow.processing_receipts")[0][0] == 0
    assert observe(core, "SELECT sum(committed_count) FROM orderflow.processing_partitions")[0][0] == 0
    assert core["client"].post(f"/v1/events/{event_id}/retry", headers=core["auth"], json={"reason": "Investigated transient fault"}).status_code == 202
    assert observe(core, "SELECT count(*) FROM orderflow.operator_audit")[0][0] == 1
    assert process_one(TENANT)["outcome"] == "applied_current"
    assert core["client"].get(f"/v1/events/{event_id}", headers=core["auth"]).json()["attempt_count"] == 2
    assert core["client"].post(f"/v1/events/{event_id}/retry", headers=core["auth"], json={"reason": "Should not reopen terminal"}).status_code == 409


def test_retry_exhaustion_preserves_one_terminal_receipt(core):
    event_id = post(core, product()).json()["event_id"]
    def fail(stage, event, conn):
        if stage == "before_materialize":
            raise RuntimeError("synthetic transient")
    for number in range(5):
        result = process_one(TENANT, fault=fail)
        if number < 4:
            assert result["status"] == "retry_wait"
            assert core["client"].post(f"/v1/events/{event_id}/retry", headers=core["auth"], json={"reason": "Reviewed repeat fault"}).status_code == 202
    assert result["status"] == "quarantined" and result["reason_code"] == "retry_exhausted"
    assert observe(core, "SELECT count(*) FROM orderflow.processing_receipts")[0][0] == 1
    assert observe(core, "SELECT sum(committed_count) FROM orderflow.processing_partitions")[0][0] == 1
    assert core["client"].get(f"/v1/events/{event_id}", headers=core["auth"]).json()["attempt_count"] == 5


def test_keyset_pages_are_complete_and_scope_bound(core):
    for number in range(31):
        body = product(f"bad-{number}", entity=f"P{number}")
        body["payload"]["currency"] = "EUR"
        assert post(core, body).status_code == 202
    drain([TENANT])
    seen = []
    cursor = None
    first_cursor = None
    while True:
        params = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        response = core["client"].get("/v1/quarantines", headers=core["auth"], params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(r["quarantine_id"] for r in page["items"])
        cursor = page["next_cursor"]
        first_cursor = first_cursor or cursor
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 31
    assert core["client"].get("/v1/quarantines", headers=core["beta"], params={"cursor": first_cursor}).status_code == 422
    assert core["client"].get("/v1/quarantines", headers=core["auth"], params={"cursor": first_cursor[:-2] + "zz"}).status_code == 422
    assert core["client"].get("/v1/events", headers=core["auth"], params={"cursor": first_cursor}).status_code == 422


def test_forced_rls_pool_context_and_composite_fk(core):
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text("SELECT count(*) FROM orderflow.sources")).scalar_one() == 3
        with pytest.raises(DBAPIError):
            conn.execute(text("INSERT INTO orderflow.processing_partitions(tenant_id,source_id,partition_key) VALUES(:tenant,:source,'product:P1')"), {"tenant": TENANT, "source": BETA_CATALOG})
    with tenant_transaction(BETA) as conn:
        assert conn.execute(text("SELECT count(*) FROM orderflow.sources")).scalar_one() == 1
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM orderflow.sources")).scalar_one() == 0


def test_immutable_terminal_records_even_for_owner(core):
    post(core, product())
    process_one(TENANT)
    for statement in ["UPDATE orderflow.entity_revisions SET payload='{}'::jsonb", "DELETE FROM orderflow.processing_receipts", "UPDATE orderflow.inbox_events SET status='pending'", "UPDATE orderflow.entity_current SET entity_version=entity_version"]:
        with pytest.raises(DBAPIError):
            with get_engine(core["admin"]).begin() as conn:
                conn.execute(text(statement))


def test_unrelated_partition_progresses_while_one_is_locked(core):
    post(core, product("locked", entity="P1"))
    post(core, product("free", entity="P2"))
    with tenant_transaction(TENANT) as conn:
        conn.execute(text("SELECT partition_key FROM orderflow.processing_partitions WHERE partition_key='product:P1' FOR UPDATE")).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(process_one, TENANT).result(timeout=8)
        assert result["status"] == "completed"
        assert conn.execute(text("SELECT entity_id FROM orderflow.entity_current")).scalar_one() == "P2"
    assert process_one(TENANT)["status"] == "completed"


def test_long_unicode_cursor_survives_current_revision_change(core):
    entities = ["界" * 199 + suffix for suffix in ("a", "b")]
    for number, entity in enumerate(entities):
        assert post(core, product(f"unicode-{number}", entity=entity)).status_code == 202
    drain([TENANT])
    first = core["client"].get("/v1/current/products", params={"limit": 1}, headers=core["auth"]).json()
    assert first["items"][0]["entity_id"] == entities[0]
    assert len(first["next_cursor"]) <= 512
    post(core, product("unicode-new-version", entity=entities[0], version=2, effective="2026-11-02T12:00:00Z"))
    drain([TENANT])
    second = core["client"].get("/v1/current/products", params={"limit": 1, "cursor": first["next_cursor"]}, headers=core["auth"])
    assert second.status_code == 200
    assert [v["entity_id"] for v in second.json()["items"]] == [entities[1]]
    assert second.json()["next_cursor"] is None


def test_opaque_history_identifier_query_form(core):
    entity = "../opaque/part"
    post(core, product(entity=entity))
    drain([TENANT])
    response = core["client"].get("/v1/product-history", params={"entity_id": entity}, headers=core["auth"])
    assert response.status_code == 200
    assert response.json()["entity_id"] == entity
    assert len(response.json()["raw_revisions"]) == 1


def test_revocation_invalidates_membership_and_source_key(core):
    with get_engine(core["admin"]).begin() as conn:
        conn.execute(text("UPDATE orderflow.memberships SET active=false WHERE subject='alpha-operator'"))
        conn.execute(text("UPDATE orderflow.source_credentials SET active=false WHERE source_id=:source"), {"source": CATALOG})
    assert core["client"].get("/v1/session", headers=core["auth"]).status_code == 403
    assert post(core, product()).status_code == 401
    assert observe(core, "SELECT count(*) FROM orderflow.inbox_events")[0][0] == 0


def test_request_budget_is_shared_and_has_retry_header(core, monkeypatch):
    monkeypatch.setenv("ODF_REQUESTS_PER_MINUTE", "1")
    assert core["client"].get("/v1/overview", headers=core["auth"]).status_code == 200
    response = core["client"].get("/v1/overview", headers=core["auth"])
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"


def test_production_fixture_login_disabled(core, monkeypatch):
    monkeypatch.setenv("ODF_APP_ENV", "production")
    monkeypatch.setenv("ODF_DEMO_MODE", "false")
    monkeypatch.setenv("ODF_ALLOWED_ORIGINS", "https://testserver")
    from fastapi.testclient import TestClient
    from orderflow.api import app
    with TestClient(app, base_url="https://testserver") as client:
        config = client.get("/v1/auth/config")
        assert config.status_code == 200
        assert config.json()["fixture_login_enabled"] is False and config.json()["identities"] == []
        response = client.post("/v1/dev/session", json={"identity_id": "alpha-operator"})
        assert response.status_code == 404 and response.headers["Strict-Transport-Security"]


def test_canonical_browser_origin_can_login_and_foreign_origin_cannot(core, monkeypatch):
    monkeypatch.delenv("ODF_ALLOWED_ORIGINS", raising=False)
    accepted = core["client"].post("/v1/dev/session", headers={"Origin": "http://127.0.0.1:8010"}, json={"identity_id": "alpha-operator"})
    assert accepted.status_code == 200
    assert accepted.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:8010"
    denied = core["client"].post("/v1/dev/session", headers={"Origin": "https://untrusted.example"}, json={"identity_id": "alpha-operator"})
    assert denied.status_code == 403


def test_configured_process_does_not_reparse_dotenv(monkeypatch):
    import dotenv
    monkeypatch.setenv("ODF_DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1/unused")
    monkeypatch.setenv("ODF_AUTH_SECRET", "synthetic-test-secret-" * 3)
    monkeypatch.setenv("ODF_APP_ENV", "test")
    monkeypatch.setenv("ODF_DEMO_MODE", "false")
    def unexpected_read(*args, **kwargs):
        raise AssertionError("A fully configured process must not re-read .env")
    monkeypatch.setattr(dotenv, "load_dotenv", unexpected_read)
    for _ in range(20):
        assert Settings.from_env().app_env == "test"


def test_initial_cli_configuration_loads_dotenv_once(monkeypatch):
    import dotenv
    monkeypatch.delenv("ODF_DATABASE_URL", raising=False)
    monkeypatch.delenv("ODF_AUTH_SECRET", raising=False)
    monkeypatch.setenv("ODF_APP_ENV", "test")
    monkeypatch.setenv("ODF_DEMO_MODE", "false")
    calls = []
    def initial_load(path, override=False):
        calls.append((path, override))
        monkeypatch.setenv("ODF_DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1/unused")
        monkeypatch.setenv("ODF_AUTH_SECRET", "synthetic-test-secret-" * 3)
    monkeypatch.setattr(dotenv, "load_dotenv", initial_load)
    assert Settings.from_env().app_env == "test"
    assert Settings.from_env().app_env == "test"
    assert calls == [(ROOT / ".env", False)]


def test_bounded_claim_prefix_skips_locks_without_scanning_later_queue(core):
    for number in range(128):
        assert post(core, product(f"busy-{number}", entity="BUSY")).status_code == 202
    free = post(core, product("outside-prefix", entity="FREE")).json()["event_id"]
    with tenant_transaction(TENANT) as conn:
        conn.execute(text("SELECT partition_key FROM orderflow.processing_partitions WHERE partition_key='product:BUSY' FOR UPDATE")).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(process_one, TENANT).result(timeout=5) is None
    # Once one prefix event finishes, the free partition enters the128-event
    # candidate window and another worker can skip every remaining busy row.
    assert process_one(TENANT)["status"] == "completed"
    with tenant_transaction(TENANT) as conn:
        conn.execute(text("SELECT partition_key FROM orderflow.processing_partitions WHERE partition_key='product:BUSY' FOR UPDATE")).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(process_one,TENANT).result(timeout=5)
    assert result["event_id"] == free and result["status"] == "completed"


def test_paused_source_prefix_cannot_starve_enabled_source(core):
    for number in range(130):
        assert post(core, product(f"paused-{number}", entity=f"PAUSED-{number}")).status_code == 202
    with get_engine(core["admin"]).begin() as conn:
        conn.execute(text("UPDATE orderflow.sources SET enabled=false WHERE source_id=:source"),{"source":CATALOG})
    row=json.loads((ROOT/'fixtures/happy_path.jsonl').read_text().splitlines()[1])
    event=post(core,row['envelope'],row['source_id']).json()['event_id']
    result=process_one(TENANT)
    assert result['event_id']==event and result['outcome']=='applied_current'
    assert observe(core,"SELECT count(*) FROM orderflow.inbox_events WHERE status='pending'")[0][0]==130
