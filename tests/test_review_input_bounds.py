"""Extreme but syntactically valid input stays inside typed failure contracts."""
from fastapi.testclient import TestClient
import pytest

from orderflow import api, service
from orderflow.auth import SourcePrincipal
from orderflow.connectors import ConnectorError, parse_inventory

TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EXTREME_TIMESTAMPS = ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"]


@pytest.fixture
def bounded_api(monkeypatch):
    monkeypatch.setenv("ODF_DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1/unused")
    monkeypatch.setenv("ODF_AUTH_SECRET", "synthetic-boundary-check-secret-" * 3)
    monkeypatch.setenv("ODF_APP_ENV", "test")
    monkeypatch.setenv("ODF_DEMO_MODE", "false")
    monkeypatch.setenv("ODF_ALLOWED_HOSTS", "testserver")
    overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.source_identity] = lambda: SourcePrincipal(TENANT, SOURCE)
    monkeypatch.setattr(service, "accept_event", lambda *_: pytest.fail("Invalid timestamps must not reach storage"))
    try:
        with TestClient(api.app) as client:
            yield client
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(overrides)


@pytest.mark.parametrize("timestamp", EXTREME_TIMESTAMPS)
def test_utc_conversion_overflow_is_an_ingress_422(bounded_api, timestamp):
    body = {"tenant_id": TENANT, "source_event_id": "overflow-fixture", "partition_key": "product:P1",
            "schema_version": "1", "entity_type": "product", "entity_id": "P1", "entity_version": 1,
            "effective_at": timestamp, "occurred_at": "2026-11-01T00:00:00Z",
            "payload": {"sku": "SKU", "name": "Fixture", "unit_price_minor": 1, "currency": "USD"}}
    response = bounded_api.post("/v1/sources/" + SOURCE + "/events", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_envelope"


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("timestamp", EXTREME_TIMESTAMPS)
def test_inventory_utc_conversion_overflow_is_a_connector_error(reference, timestamp):
    identity = "reference_id" if reference else "snapshot_id"
    raw = (f"tenant_id,{identity},as_of,sku,quantity\n{TENANT},INV,{timestamp},SKU,1\n").encode()
    with pytest.raises(ConnectorError, match="inventory_invalid"):
        parse_inventory(raw, reference=reference)
