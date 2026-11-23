"""Explicit PostgreSQL transactions and reviewed administrative setup helpers."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
from uuid import UUID

from sqlalchemy import create_engine, text

from .config import ROOT, Settings

_engines = {}
_lock = threading.Lock()


def get_engine(url=None):
    url = url or Settings.from_env().database_url
    with _lock:
        if url not in _engines:
            serverless = os.environ.get("VERCEL") == "1" or os.environ.get("ODF_SERVERLESS") == "true"
            _engines[url] = create_engine(url, pool_pre_ping=True, pool_size=1 if serverless else 8, max_overflow=1 if serverless else 8,
                                         pool_recycle=180, pool_timeout=5, connect_args={"connect_timeout": 10 if serverless else 5})
        return _engines[url]


def dispose_engines():
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


@contextmanager
def tenant_transaction(tenant_id, actor_subject="worker", role="worker"):
    tenant_id = str(UUID(str(tenant_id)))
    if role not in {"worker", "operator", "viewer", "source", "pipeline"}:
        raise ValueError("Invalid transaction role")
    with get_engine().begin() as conn:
        conn.execute(text("SELECT set_config('orderflow.tenant_id',:tenant,true),set_config('orderflow.actor_subject',:actor,true),set_config('orderflow.actor_role',:role,true),set_config('statement_timeout','15000',true),set_config('lock_timeout','3000',true),set_config('idle_in_transaction_session_timeout','20000',true)"),
                     {"tenant": tenant_id, "actor": str(actor_subject)[:300], "role": role})
        yield conn


def migrate(admin_url):
    """Apply content-addressed SQL once. Never creates a role or resets data."""
    engine = get_engine(admin_url)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(721930415)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS public.orderflow_schema_migrations(name text PRIMARY KEY,sha256 char(64) NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"))
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            previous = conn.execute(text("SELECT sha256 FROM public.orderflow_schema_migrations WHERE name=:name"), {"name": path.name}).scalar_one_or_none()
            if previous:
                if previous != digest:
                    raise RuntimeError(f"Applied migration changed: {path.name}")
                continue
            conn.connection.driver_connection.execute(raw.decode("utf-8"), prepare=False)
            conn.execute(text("INSERT INTO public.orderflow_schema_migrations(name,sha256) VALUES(:name,:sha)"), {"name": path.name, "sha": digest})


def seed(admin_url, metadata, source_keys, memberships):
    """Add reviewed identities. Preserve event history and conflicting metadata."""
    with get_engine(admin_url).begin() as conn:
        for tenant in metadata["tenants"]:
            conn.execute(text("SELECT set_config('orderflow.tenant_id',:tenant,true)"), {"tenant": str(tenant["tenant_id"])})
            conn.execute(text("INSERT INTO orderflow.tenants(tenant_id,name,currency) VALUES(:tenant_id,:name,:currency) ON CONFLICT DO NOTHING"), {k: tenant[k] for k in ("tenant_id", "name", "currency")})
        for source in metadata["sources"]:
            conn.execute(text("SELECT set_config('orderflow.tenant_id',:tenant,true)"), {"tenant": str(source["tenant_id"])})
            conn.execute(text("INSERT INTO orderflow.sources(tenant_id,source_id,name,connector_type) VALUES(:tenant_id,:source_id,:name,:connector_type) ON CONFLICT DO NOTHING"), {k: source[k] for k in ("tenant_id", "source_id", "name", "connector_type")})
        for authority in metadata["authorities"]:
            conn.execute(text("SELECT set_config('orderflow.tenant_id',:tenant,true)"), {"tenant": str(authority["tenant_id"])})
            conn.execute(text("INSERT INTO orderflow.source_authorities(tenant_id,entity_type,source_id) VALUES(:tenant_id,:entity_type,:source_id) ON CONFLICT DO NOTHING"), authority)
        source_tenants = {str(v["source_id"]): str(v["tenant_id"]) for v in metadata["sources"]}
        for source_id, raw_key in source_keys.items():
            if len(raw_key) < 32:
                raise ValueError("Source keys must contain at least 32 characters")
            conn.execute(text("INSERT INTO orderflow.source_credentials(key_sha256,tenant_id,source_id) VALUES(:sha,:tenant,:source) ON CONFLICT DO NOTHING"), {"sha": hashlib.sha256(raw_key.encode()).hexdigest(), "tenant": source_tenants[str(source_id)], "source": str(source_id)})
        for member in memberships:
            values = {k: str(member[k]) for k in ("issuer", "subject", "tenant_id", "role")}
            values["alias"] = member.get("alias")
            conn.execute(text("INSERT INTO orderflow.memberships(issuer,subject,tenant_id,role,fixture_alias) VALUES(:issuer,:subject,:tenant_id,:role,:alias) ON CONFLICT DO NOTHING"), values)


def primitive(value):
    """Convert UUID/datetime/Decimal database values without lossy money floats."""
    from datetime import datetime, timezone
    from decimal import Decimal
    if isinstance(value, dict):
        return {k: primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value
