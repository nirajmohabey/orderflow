"""Validated configuration. Importing this module performs no I/O."""
from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str
    demo_mode: bool
    auth_secret: str
    jwt_issuer: str
    jwt_audience: str
    jwt_algorithm: str
    jwt_public_key: str
    max_body_bytes: int
    body_timeout: float
    max_attempts: int
    requests_per_minute: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    worker_tenants: tuple[str, ...]
    neon_auth_url: str = ""

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        # Load a local file only for initial CLI/dev configuration. A configured
        # server/worker validates its explicit environment without repeatedly
        # parsing disk on every request layer. File edits require a restart.
        if "ODF_DATABASE_URL" not in os.environ or "ODF_AUTH_SECRET" not in os.environ:
            load_dotenv(ROOT / ".env", override=False)
        def integer(name, default, low, high):
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                raise RuntimeError(f"{name} must be an integer") from None
            if not low <= value <= high:
                raise RuntimeError(f"{name} is outside its permitted range")
            return value
        app_env = os.environ.get("ODF_APP_ENV", "production")
        if app_env not in {"local", "test", "production"}:
            raise RuntimeError("ODF_APP_ENV must be local, test or production")
        demo = os.environ.get("ODF_DEMO_MODE", "false").lower() == "true"
        if demo and app_env != "local":
            raise RuntimeError("Fixture login requires explicit local mode")
        url = os.environ.get("ODF_DATABASE_URL", "")
        if not url.startswith("postgresql+psycopg://"):
            raise RuntimeError("ODF_DATABASE_URL must use PostgreSQL/psycopg")
        secret = os.environ.get("ODF_AUTH_SECRET", "")
        if len(secret.encode()) < 32:
            raise RuntimeError("ODF_AUTH_SECRET requires at least 32 bytes")
        from .neon_auth import configuration
        neon = configuration()
        algorithm = "EdDSA" if neon else os.environ.get("ODF_JWT_ALGORITHM", "HS256")
        if algorithm not in {"HS256", "RS256", "EdDSA"}:
            raise RuntimeError("ODF_JWT_ALGORITHM must be HS256, RS256 or EdDSA")
        public_key = os.environ.get("ODF_JWT_PUBLIC_KEY", "").replace("\\n", "\n")
        if algorithm == "RS256" and not public_key.startswith("-----BEGIN PUBLIC KEY-----"):
            raise RuntimeError("RS256 requires a configured public key")
        hosts = tuple(v.strip() for v in os.environ.get("ODF_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if v.strip())
        origins = tuple(v.strip() for v in os.environ.get("ODF_ALLOWED_ORIGINS", "http://127.0.0.1:8010,http://localhost:8010").split(",") if v.strip())
        if os.environ.get("VERCEL") == "1" and app_env == "production":
            platform_hosts = []
            for name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
                value = os.environ.get(name, "").lower()
                if value and all(c.isalnum() or c in ".-" for c in value) and "." in value:
                    platform_hosts.append(value)
            hosts = tuple(dict.fromkeys((*hosts, *platform_hosts)))
            origins = tuple(dict.fromkeys((*origins, *("https://" + host for host in platform_hosts))))
        if not hosts or "*" in hosts or "*" in origins:
            raise RuntimeError("Exact host and origin allowlists are required")
        if app_env == "production" and any(urlsplit(v).scheme != "https" or urlsplit(v).path not in {"", "/"} for v in origins):
            raise RuntimeError("Production origins require exact HTTPS origins")
        try:
            timeout = float(os.environ.get("ODF_BODY_TIMEOUT_SECONDS", "5"))
            if not 0.1 <= timeout <= 30:
                raise ValueError
            tenants = tuple(str(UUID(v.strip())) for v in os.environ.get("ODF_WORKER_TENANTS", "").split(",") if v.strip())
        except ValueError:
            raise RuntimeError("Invalid body timeout or worker tenant configuration") from None
        return cls(url, app_env, demo, secret,
                   neon["origin"] if neon else os.environ.get("ODF_JWT_ISSUER", "https://orderflow.local.invalid"),
                   os.environ.get("ODF_JWT_AUDIENCE", "orderflow-api"), algorithm, public_key,
                   integer("ODF_REQUEST_MAX_BYTES", 65536, 1024, 1048576), timeout,
                   integer("ODF_MAX_ATTEMPTS", 5, 1, 10),
                   integer("ODF_REQUESTS_PER_MINUTE", 6000, 1, 1000000), hosts, origins, tenants, neon["base"] if neon else "")
