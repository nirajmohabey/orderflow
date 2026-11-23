"""Authenticated API with bounded durable acceptance and a static dashboard."""
from contextlib import asynccontextmanager
import asyncio
import logging
import os
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from . import __version__, service
from .auth import AuthError, Principal, SourcePrincipal, authenticate_source, fixture_identity, issue_fixture_token, verify_bearer
from .config import ROOT, Settings
from .db import get_engine
from .schemas import ExactModel, RetryRequest, parse_envelope, strict_json

log = logging.getLogger("orderflow.http")
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app):
    Settings.from_env()
    yield


app = FastAPI(title="OrderFlow", version=__version__, lifespan=lifespan,
              docs_url="/docs" if os.environ.get("ODF_APP_ENV") == "local" else None,
              redoc_url=None, description="Durable event identity, transactional projections and explicit quality gates. Synthetic demo; no exactly-once delivery claim.")


def error_response(request, status, code, message):
    response = JSONResponse({"code": code, "message": message, "request_id": getattr(request.state, "request_id", str(uuid4()))}, status_code=status)
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    if status == 429:
        response.headers["Retry-After"] = "60"
    return response


@app.middleware("http")
async def request_boundaries(request, call_next):
    request.state.request_id = str(uuid4())
    settings = Settings.from_env()
    try:
        if (request.url.hostname or "").lower() not in settings.allowed_hosts:
            raise service.DomainError(400, "invalid_host", "The request host is not allowed.")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in settings.allowed_origins:
            raise service.DomainError(403, "origin_forbidden", "The browser origin is not allowed.")
        if settings.app_env == "production" and request.url.path not in {"/healthz", "/health/ready"} and request.url.scheme != "https":
            raise service.DomainError(400, "https_required", "HTTPS is required.")
        if len(request.headers.get("authorization", "")) > 16400:
            raise AuthError()
        lengths = [value for key, value in request.scope.get("headers", []) if key.lower() == b"content-length"]
        if len(lengths) > 1:
            raise service.DomainError(400, "ambiguous_request_length", "Multiple content-length headers are forbidden.")
        length = request.headers.get("content-length", "")
        if length and (not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > settings.max_body_bytes):
            raise service.DomainError(413, "body_too_large", "The request body exceeds the configured byte limit.")
        if request.method in {"POST", "PUT", "PATCH"}:
            if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                raise service.DomainError(415, "unsupported_media_type", "API bodies require application/json.")
            body = bytearray()
            async with asyncio.timeout(settings.body_timeout):
                async for chunk in request.stream():
                    if len(chunk) > settings.max_body_bytes - len(body):
                        raise service.DomainError(413, "body_too_large", "The request body exceeds the configured byte limit.")
                    body.extend(chunk)
            request._body = bytes(body)
            try:
                strict_json(request._body)
            except (ValueError, UnicodeError, RecursionError):
                raise service.DomainError(422, "invalid_json", "The body must contain valid unambiguous JSON within the request limits.") from None
        response = await call_next(request)
    except (service.DomainError, AuthError) as error:
        response = error_response(request, error.status, error.code, error.message)
    except SQLAlchemyError:
        response = error_response(request, 503, "storage_unavailable", "The database is temporarily unavailable. Retry the same source event identity.")
    except TimeoutError:
        response = error_response(request, 408, "request_timeout", "The body was not received in time.")
    except ClientDisconnect:
        response = error_response(request, 400, "incomplete_request", "The request body was incomplete.")
    except Exception as error:
        log.error("Unexpected request failure (%s)", type(error).__name__)
        response = error_response(request, 500, "internal_error", "The request could not be completed.")
    response.headers.update({"X-Request-ID": request.state.request_id, "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "same-origin", "Cache-Control": "no-store",
                             "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(),microphone=(),geolocation=()"})
    # Local Swagger needs its own external assets; production disables docs.
    if request.url.path != "/docs":
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") in settings.allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin.rstrip("/")
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "X-Request-ID,Retry-After"
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    # Log operation/status only, not payloads, identifiers, authorization or SQL.
    log.info("request=%s method=%s status=%s", request.state.request_id, request.method, response.status_code)
    return response


@app.exception_handler(service.DomainError)
@app.exception_handler(AuthError)
async def expected_error(request, error):
    return error_response(request, error.status, error.code, error.message)


@app.exception_handler(RequestValidationError)
async def invalid_input(request, error):
    return error_response(request, 422, "invalid_input", "The request does not match the API contract.")


@app.exception_handler(SQLAlchemyError)
async def unavailable(request, error):
    return error_response(request, 503, "storage_unavailable", "The database is temporarily unavailable. Retry the same source event identity.")


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return error_response(request, error.status_code, "not_found" if error.status_code == 404 else "http_error", "The requested resource was not found." if error.status_code == 404 else "The HTTP operation is not permitted.")


def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), tenant_id: str | None = Header(default=None, alias="X-Tenant-ID")):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError()
    p = verify_bearer(credentials.credentials, tenant_id)
    service.rate_limit(p.tenant_id, p.subject, "api")
    return p


def source_identity(source_id: UUID, key: str | None = Header(default=None, alias="X-Source-Key")):
    p = authenticate_source(key, source_id)
    service.rate_limit(p.tenant_id, p.source_id, "ingest")
    return p


@app.options("/v1/{path:path}", include_in_schema=False)
def preflight(path: str, request: Request):
    wanted = {v.strip().lower() for v in request.headers.get("access-control-request-headers", "").split(",") if v.strip()}
    if not request.headers.get("origin") or request.headers.get("access-control-request-method") not in {"GET", "POST"} or not wanted <= {"authorization", "content-type", "x-tenant-id", "x-source-key"}:
        raise service.DomainError(403, "cors_forbidden", "The browser operation is not permitted.")
    return Response(status_code=204, headers={"Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Tenant-ID,X-Source-Key", "Access-Control-Max-Age": "600"})


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with get_engine().connect() as conn:
        role = conn.execute(text("SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=current_user")).mappings().one()
        owned = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='orderflow' AND tableowner=current_user")).scalar_one()
        tables = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='orderflow' AND tablename IN('inbox_events','processing_receipts','entity_revisions','entity_current')")).scalar_one()
        unprotected = conn.execute(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='orderflow' AND c.relname IN('inbox_events','processing_receipts','entity_revisions','entity_current') AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)")).scalar_one()
        if any(role.values()) or owned or tables != 4 or unprotected:
            raise service.DomainError(503, "unsafe_database_configuration", "The required nonowner tenant-isolated database configuration is unavailable.")
    return {"status": "ready", "database_role": "nonowner_rls"}


@app.get("/v1/auth/config")
def auth_config():
    settings = Settings.from_env()
    enabled = settings.app_env == "local" and settings.demo_mode and settings.jwt_algorithm == "HS256"
    identities = []
    if enabled:
        for alias in ("alpha-operator", "alpha-viewer", "beta-operator", "beta-viewer"):
            try:
                p = fixture_identity(alias)
                identities.append({"id": alias, "label": alias.replace("-", " · ").title(), "tenant_id": p.tenant_id, "role": p.role})
            except AuthError:
                continue
    return {"mode": "fixture" if enabled else "bearer", "fixture_login_enabled": enabled, "identities": identities}


class FixtureLogin(ExactModel):
    identity_id: str


@app.post("/v1/dev/session")
def demo_login(body: FixtureLogin):
    p = fixture_identity(body.identity_id)
    service.rate_limit(p.tenant_id, "fixture:" + p.subject, "login")
    return {"access_token": issue_fixture_token(p), "token_type": "bearer", "principal": p.as_dict(), "tenant": service.tenant_info(p), "demo": True}


@app.get("/v1/session")
def session(p: Principal = Depends(identity)):
    return {"principal": p.as_dict(), "tenant": service.tenant_info(p)}


@app.post("/v1/sources/{source_id}/events", status_code=202)
async def ingest(source_id: UUID, request: Request, p: SourcePrincipal = Depends(source_identity)):
    try:
        envelope = parse_envelope(await request.body())
    except (ValidationError, ValueError, UnicodeError):
        raise service.DomainError(422, "invalid_envelope", "The event does not match the typed envelope contract.") from None
    return await run_in_threadpool(service.accept_event, p, envelope)


@app.get("/v1/events/{event_id}")
def event(event_id: UUID, p: Principal = Depends(identity)):
    return service.get_event(p, event_id)


@app.post("/v1/events/{event_id}/retry", status_code=202)
def retry(event_id: UUID, body: RetryRequest, p: Principal = Depends(identity)):
    service.rate_limit(p.tenant_id, p.subject, "operator_retry")
    return service.retry_event(p, event_id, body.reason)


@app.get("/v1/sources/{source_id}/status")
def source_status(source_id: UUID, p: Principal = Depends(identity)):
    return service.source_status(p, source_id)


@app.get("/v1/runs/{run_id}")
def run(run_id: UUID, p: Principal = Depends(identity)):
    return service.get_run(p, run_id)


@app.get("/v1/runs")
def runs(limit: int = Query(20, ge=1, le=100), p: Principal = Depends(identity)):
    return service.list_runs(p, limit)


@app.get("/v1/quarantines")
def quarantines(limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, max_length=512), p: Principal = Depends(identity)):
    return service.list_quarantines(p, limit, cursor)


@app.get("/v1/overview")
def overview(p: Principal = Depends(identity)):
    return service.overview(p)


@app.get("/v1/sources")
def sources(p: Principal = Depends(identity)):
    return service.list_sources(p)


@app.get("/v1/events")
def events(limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, max_length=512), status: str | None = None, source_id: UUID | None = None, p: Principal = Depends(identity)):
    return service.list_events(p, limit, cursor, status, source_id)


@app.get("/v1/current/products")
def products(limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, max_length=512), p: Principal = Depends(identity)):
    return service.list_current(p, "product", limit, cursor)


@app.get("/v1/current/orders")
def orders(limit: int = Query(25, ge=1, le=100), cursor: str | None = Query(None, max_length=512), p: Principal = Depends(identity)):
    return service.list_current(p, "order", limit, cursor)


@app.get("/v1/products/{entity_id:path}/history")
def history(entity_id: str, p: Principal = Depends(identity)):
    return service.product_history(p, entity_id)


@app.get("/v1/product-history")
def history_by_query(entity_id: str = Query(min_length=1, max_length=200), p: Principal = Depends(identity)):
    # Query form also handles dot segments and slashes without browser URL
    # normalization changing an otherwise valid opaque entity identifier.
    return service.product_history(p, entity_id)


@app.get("/v1/reports/latest")
def latest_report(p: Principal = Depends(identity)):
    from .pipeline import read_latest_report
    return read_latest_report(p.tenant_id)


@app.get("/v1/inventory/latest")
def latest_inventory(source_id: UUID | None = None, p: Principal = Depends(identity)):
    from .pipeline import read_inventory_latest
    return read_inventory_latest(p.tenant_id, source_id)


@app.get("/v1/inventory/history")
def inventory_history(source_id: UUID | None = None, limit: int = Query(10, ge=1, le=20), p: Principal = Depends(identity)):
    from .pipeline import read_inventory_history
    return read_inventory_history(p.tenant_id, source_id, limit)


dist = ROOT / "dashboard/dist"
if (dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    if not (dist / "favicon.svg").is_file():
        raise service.DomainError(404, "not_found", "The requested resource was not found.")
    return FileResponse(dist / "favicon.svg", media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def dashboard():
    if not (dist / "index.html").is_file():
        raise service.DomainError(503, "dashboard_not_built", "Build the dashboard before opening the workbench.")
    return FileResponse(dist / "index.html", media_type="text/html")
