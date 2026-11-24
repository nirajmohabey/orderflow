"""Synthetic noncanonical fixture through the existing canonical connector path."""
from copy import deepcopy
import importlib
import json
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import text

from test_core import core, TENANT, BETA, CATALOG
from orderflow.config import ROOT
from orderflow.connectors import ConnectorError, catalog_app, ingest_catalog, read_cursor
from orderflow.db import tenant_transaction

FIXTURE = ROOT / "fixtures/customer_mapping/maple_catalog.json"
EXPECTED = ROOT / "fixtures/customer_mapping/maple_catalog.expected.jsonl"


@pytest.fixture
def mapper(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("customer_catalog_mapping")


def document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def mapped(mapper, data=None, **config):
    return mapper.map_catalog(json.dumps(document() if data is None else data).encode(),
                              tenant_id=config.get("tenant_id", TENANT),
                              merchant_code=config.get("merchant_code", "maple-demo"))


def test_noncanonical_mapping_matches_independent_literal_canonical_fixture(mapper):
    expected = [json.loads(line) for line in EXPECTED.read_text().splitlines()]
    actual = mapped(mapper)
    assert actual == expected
    assert actual[0]["source_event_id"] == "maple-catalog-017"
    assert actual[0]["effective_at"] == "2026-11-02T13:30:00.000000Z"
    assert actual[2]["effective_at"] == "2026-11-02T13:15:00.000000Z"
    assert actual[0]["payload"]["unit_price_minor"] == 1230
    assert actual[2]["payload"]["unit_price_minor"] == 10
    assert mapped(mapper) == actual, "Replaying the export must not invent new source identities"


@pytest.mark.parametrize("field,value", [
    ("unit_price", 12.3), ("unit_price", "12.301"), ("unit_price", "1e2"),
    ("unit_price", "-1.00"), ("unit_price", "NaN"), ("unit_price", "1,000.00"),
    ("unit_price", "10000000000000.01"), ("currency", "EUR"),
    ("valid_from", "2026-11-02T08:30:00"), ("revision", True),
    ("revision", 0), ("notification_id", ""), ("product_code", ""),
    ("unexpected_tenant_id", BETA),
])
def test_mapping_rejects_ambiguous_or_unreviewed_record_before_ingress(mapper, field, value):
    data = document()
    data["catalog_updates"][0][field] = value
    with pytest.raises(ValueError):
        mapped(mapper, data)


@pytest.mark.parametrize("mutation", ["merchant", "unknown_root", "empty", "too_many", "wrong_container"])
def test_mapping_rejects_untrusted_document_and_oversized_batches(mapper, mutation):
    data = document()
    if mutation == "merchant": data["merchant_code"] = "another-customer"
    if mutation == "unknown_root": data["tenant_id"] = BETA
    if mutation == "empty": data["catalog_updates"] = []
    if mutation == "too_many": data["catalog_updates"] = [data["catalog_updates"][0]] * 1001
    if mutation == "wrong_container": data["catalog_updates"] = {"record": data["catalog_updates"][0]}
    with pytest.raises(ValueError):
        mapped(mapper, data)


def test_mapping_strict_input_size_duplicate_keys_and_config(mapper):
    for raw in (b"", b"x" * (mapper.MAX_INPUT_BYTES + 1),
                b'{"merchant_code":"maple-demo","merchant_code":"maple-demo","catalog_updates":[]}'):
        with pytest.raises(ValueError):
            mapper.map_catalog(raw, tenant_id=TENANT, merchant_code="maple-demo")
    with pytest.raises(ValueError): mapped(mapper, tenant_id="not-a-uuid")
    with pytest.raises(ValueError): mapped(mapper, merchant_code="../merchant")


def test_mapping_cli_is_offline_and_emits_existing_jsonl_contract():
    child = subprocess.run([sys.executable, str(ROOT / "scripts/customer_catalog_mapping.py"),
                            "--input", str(FIXTURE), "--tenant", TENANT, "--merchant", "maple-demo"],
                           capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert [json.loads(line) for line in child.stdout.splitlines()] == [json.loads(line) for line in EXPECTED.read_text().splitlines()]


def test_mapped_catalog_real_http_cursor_replay_late_revision_and_conflict(core, mapper):
    from orderflow.api import app
    from orderflow.worker import drain
    from test_connectors import real_http

    records = mapped(mapper)
    stopped = []
    def partial_page(count, receipt):
        stopped.append(receipt["event_id"])
        if count == 1: raise RuntimeError("simulated_mapper_consumer_interruption")

    with real_http(app) as api, real_http(catalog_app(records, page_size=2)) as catalog, httpx.Client(trust_env=False) as client:
        with pytest.raises(RuntimeError, match="simulated_mapper_consumer_interruption"):
            ingest_catalog(TENANT, CATALOG, core["keys"][CATALOG], catalog, api, client=client, after_item=partial_page)
        assert read_cursor(TENANT, CATALOG) == ({}, 0)
        resumed = ingest_catalog(TENANT, CATALOG, core["keys"][CATALOG], catalog, api, client=client)
        assert resumed == {"pages": 2, "accepted": 3, "duplicates": 1, "completed": True}
        assert read_cursor(TENANT, CATALOG)[1] == 2
        replay = ingest_catalog(TENANT, CATALOG, core["keys"][CATALOG], catalog, api, client=client, refresh=True)
        assert replay == {"pages": 2, "accepted": 3, "duplicates": 3, "completed": True}
        cursor_before_conflict = read_cursor(TENANT, CATALOG)
    drain([TENANT])
    with tenant_transaction(TENANT) as conn:
        assert conn.execute(text("SELECT count(*) FROM orderflow.inbox_events")).scalar_one() == 3
        assert conn.execute(text("SELECT count(*) FROM orderflow.entity_revisions")).scalar_one() == 3
        current = conn.execute(text("SELECT entity_id,entity_version,(payload->>'unit_price_minor')::bigint FROM orderflow.current_entities ORDER BY entity_id")).all()
        assert [tuple(row) for row in current] == [("MAPLE-7", 2, 1230), ("MAPLE-8", 1, 10)]
        assert str(conn.execute(text("SELECT event_id FROM orderflow.inbox_events WHERE source_event_id='maple-catalog-017'")).scalar_one()) == stopped[0]
    with tenant_transaction(BETA) as conn:
        assert conn.execute(text("SELECT count(*) FROM orderflow.current_entities")).scalar_one() == 0
    changed = deepcopy(records)
    changed[0]["payload"]["unit_price_minor"] = 1231
    with real_http(app) as api, real_http(catalog_app(changed, page_size=2)) as catalog, httpx.Client(trust_env=False) as client:
        with pytest.raises(ConnectorError, match="http_acceptance_unconfirmed"):
            ingest_catalog(TENANT, CATALOG, core["keys"][CATALOG], catalog, api, client=client, refresh=True)
    assert read_cursor(TENANT, CATALOG) == cursor_before_conflict
