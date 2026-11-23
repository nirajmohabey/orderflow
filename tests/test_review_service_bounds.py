"""Fresh owned PostgreSQL checks for ordinary request-budget retention."""
import time

from sqlalchemy import text

from test_core import core, product, post, TENANT, BETA, CATALOG
from orderflow import service
from orderflow.auth import SourcePrincipal, verify_bearer
from orderflow.db import tenant_transaction
from orderflow.schemas import Envelope
from orderflow.worker import drain, process_one


def test_low_volume_requests_expire_only_old_same_tenant_budgets(core, monkeypatch):
    bucket = int(time.time() // 60)
    monkeypatch.setattr(service.time, "time", lambda: bucket * 60 + 30)
    for tenant in (TENANT, BETA):
        with tenant_transaction(tenant) as conn:
            conn.execute(text("""INSERT INTO orderflow.request_budgets(tenant_id,budget_key,minute_bucket,used)
                VALUES(:tenant,repeat('a',64),:old,1),(:tenant,repeat('b',64),:retained,1)"""),
                {"tenant": tenant, "old": bucket - 3, "retained": bucket - 2})
    service.rate_limit(TENANT, "low-volume-review", "api")
    for tenant, expected in ((TENANT, 0), (BETA, 1)):
        with tenant_transaction(tenant) as conn:
            assert conn.execute(text("SELECT count(*) FROM orderflow.request_budgets WHERE minute_bucket=:old"),
                                {"old": bucket - 3}).scalar_one() == expected
            assert conn.execute(text("SELECT count(*) FROM orderflow.request_budgets WHERE minute_bucket=:retained"),
                                {"retained": bucket - 2}).scalar_one() == 1


def test_history_raw_revisions_and_intervals_share_one_snapshot(core, monkeypatch):
    assert post(core, product("snapshot-v1")).status_code == 202
    drain([TENANT])
    reader = verify_bearer(core["auth"]["Authorization"].removeprefix("Bearer "))
    original = service._rows
    appended = False

    def append_after_read(conn, sql, params):
        nonlocal appended
        rows = original(conn, sql, params)
        if "orderflow.entity_revisions" in sql and not appended:
            appended = True
            envelope = Envelope.model_validate(product("snapshot-v2", version=2, effective="2026-11-02T12:00:00Z"))
            service.accept_event(SourcePrincipal(TENANT, CATALOG), envelope)
            assert process_one(TENANT)["status"] == "completed"
        return rows

    monkeypatch.setattr(service, "_rows", append_after_read)
    result = service.product_history(reader, "P1")
    assert appended
    assert [row["entity_version"] for row in result["raw_revisions"]] == [1]
    assert [row["entity_version"] for row in result["intervals"]] == [1]
    assert result["intervals"][0]["valid_to"] is None
    assert result["intervals"][0]["valid_from"].endswith("Z")
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text("SELECT max(entity_version) FROM orderflow.entity_revisions")).scalar_one() == 2


def test_history_budget_rejects_overflow_without_truncating(core, monkeypatch):
    monkeypatch.setattr(service, "MAX_HISTORY_REVISIONS", 2)
    for version in (1, 2, 3):
        assert post(core, product("budget-" + str(version), version=version)).status_code == 202
    drain([TENANT])
    response = core["client"].get("/v1/product-history", params={"entity_id": "P1"}, headers=core["auth"])
    assert response.status_code == 422
    assert response.json()["code"] == "history_budget_exceeded"
