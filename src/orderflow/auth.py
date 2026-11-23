"""Credentials establish context; token-supplied roles grant no authority."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from uuid import UUID

import jwt
from sqlalchemy import text

from .config import Settings
from .db import get_engine


class AuthError(Exception):
    def __init__(self, status=401, code="invalid_credentials", message="Valid credentials are required."):
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    role: str

    def as_dict(self):
        return {"tenant_id": self.tenant_id, "subject": self.subject, "role": self.role}


@dataclass(frozen=True)
class SourcePrincipal:
    tenant_id: str
    source_id: str


def authenticate_source(raw_key, source_id):
    if not isinstance(raw_key, str) or not 32 <= len(raw_key) <= 256:
        raise AuthError()
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    with get_engine().begin() as conn:
        row = conn.execute(text("SELECT * FROM orderflow.resolve_source(:hash,CAST(:source AS uuid))"), {"hash": digest, "source": str(source_id)}).mappings().one_or_none()
    if row is None:
        raise AuthError()
    return SourcePrincipal(str(row["tenant_id"]), str(row["source_id"]))


def verify_bearer(token, requested_tenant_id=None):
    settings = Settings.from_env()
    if not isinstance(token, str) or len(token) > 16384:
        raise AuthError()
    try:
        tenant = str(UUID(str(requested_tenant_id))) if requested_tenant_id else None
        if settings.neon_auth_url:
            from .neon_auth import decode_token
            claims = decode_token(token)
        else:
            claims = jwt.decode(token, settings.auth_secret if settings.jwt_algorithm == "HS256" else settings.jwt_public_key,
                                algorithms=[settings.jwt_algorithm], issuer=settings.jwt_issuer,
                                audience=settings.jwt_audience, leeway=0,
                                options={"require": ["iss", "sub", "aud", "exp", "iat"], "verify_iat": True})
        subject = claims["sub"]
        if not isinstance(subject, str) or not 1 <= len(subject) <= 200:
            raise ValueError
    except (jwt.PyJWTError, ValueError, TypeError):
        raise AuthError() from None
    with get_engine().begin() as conn:
        issuer = claims["iss"] if settings.neon_auth_url else settings.jwt_issuer
        rows = conn.execute(text("SELECT * FROM orderflow.resolve_member(:issuer,:subject,CAST(:tenant AS uuid))"), {"issuer": issuer, "subject": subject, "tenant": tenant}).mappings().all()
    if len(rows) != 1:
        raise AuthError(403, "membership_required", "An active membership in the requested tenant is required.")
    row = rows[0]
    return Principal(str(row["tenant_id"]), row["subject"], row["role"])


def fixture_identity(alias):
    settings = Settings.from_env()
    if settings.app_env != "local" or not settings.demo_mode or settings.jwt_algorithm != "HS256":
        raise AuthError(404, "not_found", "The requested resource was not found.")
    if alias not in {"alpha-operator", "alpha-viewer", "beta-operator", "beta-viewer"}:
        raise AuthError()
    with get_engine().begin() as conn:
        row = conn.execute(text("SELECT * FROM orderflow.fixture_member(:alias)"), {"alias": alias}).mappings().one_or_none()
    if row is None or row["issuer"] != settings.jwt_issuer:
        raise AuthError()
    return Principal(str(row["tenant_id"]), row["subject"], row["role"])


def issue_fixture_token(principal):
    settings = Settings.from_env()
    if settings.app_env != "local" or not settings.demo_mode or settings.jwt_algorithm != "HS256":
        raise AuthError(404, "not_found", "The requested resource was not found.")
    now = datetime.now(timezone.utc)
    return jwt.encode({"iss": settings.jwt_issuer, "aud": settings.jwt_audience,
                       "sub": principal.subject, "iat": now, "exp": now + timedelta(minutes=30)},
                      settings.auth_secret, algorithm="HS256")
