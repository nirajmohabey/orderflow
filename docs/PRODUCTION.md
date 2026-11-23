# Vercel and Neon runtime

The stable public deployment is [orderflow-hazel.vercel.app](https://orderflow-hazel.vercel.app). Both provider resources use their free plans. GitHub source is public and pushes to `main` automatically deploy through the approved Vercel connection.

The production implementation uses FastAPI on Vercel Functions, Neon PostgreSQL,
Neon Managed Auth and native Python Vercel Queues. A successful build alone does
not establish that authentication, queue delivery or report publication works on
the deployed alias. Retain those checks as separate, source-bound hosted proof.
The original failed large benchmark and its `not_verified` gate remain unchanged.

`app.py` exposes the existing ASGI application. `queue_worker.py` is the
file-discovered subscriber entrypoint registered through
`[[tool.vercel.subscribers]]`; its imported `@subscribe` consumer is a private
Queue handler. There is no public HTTP worker-processing endpoint. The build
compiles `dashboard/dist` and performs no migration, seed or worker startup.
Python is constrained to 3.12 and exact runtime dependencies include dbt and
`vercel-queue[typed]`. Inspect the actual function trace and uncompressed bundle
size; test data, reports, local environments and development scripts are excluded.

## Configuration and database roles

Configure these server-side values in the dedicated Vercel project. Keep values
out of logs and use separate projects/databases for development and production.

| Variable | Purpose |
| --- | --- |
| `ODF_APP_ENV=production`, `ODF_DEMO_MODE=false` | Disable fixture login |
| `ODF_DATABASE_URL` | Pooled Neon URL for restricted `orderflow_app`, SQLAlchemy `postgresql+psycopg` scheme |
| `ODF_DBT_DATABASE_URL` | Direct Neon URL for restricted `orderflow_transform`; dbt uses connection startup tenant settings |
| `ODF_AUTH_SECRET` | Independent random secret of at least 32 bytes; required configuration, never the Neon identity source |
| `NEON_AUTH_URL` | Full fixed HTTPS Neon Auth base ending in `/auth` |
| `NEON_AUTH_JWKS_URL` | Same base plus `/.well-known/jwks.json` |
| `ODF_ALLOWED_HOSTS`, `ODF_ALLOWED_ORIGINS` | Exact production hostname and HTTPS origin; no wildcards |
| `ODF_WORKER_TENANTS` | Explicit comma-separated synthetic tenant UUID allowlist |
| `ODF_QUEUE_ENABLED=true` | Enable the transactional hosted job outbox |
| `ODF_OBJECT_STORAGE=postgres` | Store immutable report/source objects in Neon |
| `ODF_SERVERLESS=true` | Small connection pools and temporary dbt work |
| `CRON_SECRET` | Independent random secret of at least 32 bytes for outbox dispatch repair |

Vercel's exact `VERCEL_URL` and `VERCEL_PROJECT_PRODUCTION_URL` hosts are accepted
only when the platform sets `VERCEL=1`; arbitrary forwarded hosts are not trusted.
Use the platform Queue OIDC credentials rather than a browser Queue token.

Run migrations `001` through `005` once with a reviewed owner connection outside
the HTTP project's environment. SQL-created runtime and transformation roles
must be nonowners without `SUPERUSER`, `BYPASSRLS`, `CREATEDB` or `CREATEROLE`.
The transform role owns only `orderflow_warehouse` and receives tenant-filtered
source SELECT grants. The Vercel project receives runtime and transform role
connections, never the migration/administrative owner URL. Both functions share
project environment variables, so the transform connection remains a secret of
the same restricted deployment boundary.

Migration `005` adds forced-RLS `stored_objects` and `hosted_jobs`. Bytea objects
are limited to 8 MiB each, SHA-256 checked in PostgreSQL and immutable, including
against the owner trigger path. They consume the Neon database storage allowance;
there is no implicit retention purge. Watch storage and the deliberately bounded
20 non-drain job submissions per tenant per day. Source and report objects are
tenant scoped and returned by authenticated report APIs, not a public object URL.

Initialize only reviewed synthetic tenant/source metadata with fresh source
keys. Do not run local `populate_demo.py` against production. After an actual
verified Neon Auth sign-in, an administrator can register the observed exact
issuer and subject in server-side tenant memberships. Signup never grants a
tenant or operator role, and JWT role/tenant claims are ignored. Keep source
keys separate from human authentication and never expose them in the dashboard.

## Authentication boundary

`GET /v1/auth/config` returns `mode: "neon"`, `configured: true` and
`auth_base_url: "/auth"` when configured. The frontend uses the official
Better Auth client against this same-origin base and explicitly requests a JWT
from `/auth/token`. The opaque session token is not accepted as an API JWT.
The JWT stays in memory. A valid identity without a registered membership
receives the existing enrollment error rather than an automatic role.

The proxy has a fixed configured upstream, a small operation/method allowlist,
bounded bodies and responses, no followed redirects and exact callback/Location
origin checks. Cookie forwarding includes only Better Auth/Neon Auth cookies;
deployment-protection and unrelated cookies never reach Neon. Upstream cookies
become host-only `/auth` cookies (`__Host-` keeps `/`) while security attributes
are preserved. Browser POSTs require the configured origin. Signup, email OTP
verification and OTP password reset use the provider's actual endpoints; signed
reset links remain bounded fallback routes. Email delivery and verification
settings are provider account controls and need actual delivered-code checks.

API JWT verification permits only EdDSA with an Ed25519 public JWK, the exact
Auth origin as issuer and audience, required subject/expiry/issued-at claims,
bounded key sets and a bounded cached refresh/failure cooldown. Existing local
HS256 and separately configured RS256 behavior remain available; production
fixture login remains disabled.

Provider cookie sessions and API bearer tokens have different lifecycles. In the
actual logout check, the provider session disappeared and anonymous API access
returned 401, while an already-issued signed JWT remained usable through its
900-second lifetime while membership was active. The browser clears its RAM-only
bearer on logout. Provider session revocation settings do not promise immediate
invalidation of already-issued JWTs; deactivate the server-maintained membership
to remove API access immediately. No cookie or bearer value is retained in the
published lifecycle proof.

## Finite jobs and recovery

HTTP acceptance writes inbox data and the coalesced drain job in one database
transaction, then publishes a Queue wakeup. Queue send failure retains the
database outbox. The private consumer drains at most 50 events or ten seconds
per invocation and schedules another generation if work remains. Receipt and
partition locking retain the original once-only materialization behavior.

Operator-only `POST /v1/jobs/daily` and `POST /v1/jobs/inventory` require a UUID
`Idempotency-Key`. A reused key with different input conflicts. Job status is
available at `/v1/jobs/{job_id}` within the authenticated tenant. Payloads are
bounded and cannot name arbitrary files or commands.

Daily work proceeds through separate stage, check and publish deliveries.
Staging snapshots and immutable objects persist in Neon. The check phase runs
actual dbt `run` and `test`, each with a 90-second subprocess bound; all required
tests must pass before publication. Working files live in an owned temporary
directory and are cleaned after the attempt. Attempt-specific table aliases and
manifest keys prevent stale workers from overwriting another attempt. The
publication transaction checks the database lease token before updating the
last-good pointer. A failed candidate retains the previous successful report.
Inventory comparison uses the same durable store and fenced publication;
execution success and comparison failure are reported separately.

Vercel's Python runtime does not provide the process-shared semaphore used by
dbt's default context. The finite hosted dbt child therefore uses a reviewed,
version-guarded thread context and thread-pool binding. It preserves two model
threads, real SQL and all tests; it does not patch global multiprocessing in the
API, Airflow or normal CLI. The exact locked Python/dbt versions are checked
before starting. Upgrades require review and the real successful/failed SQL
regression with process semaphores deliberately unavailable. See
[dbt threads](https://docs.getdbt.com/docs/running-a-dbt-project/using-threads)
and [Python thread locks](https://docs.python.org/3.12/library/threading.html#lock-objects).

The Queue consumer has concurrency two and bounded redelivery; each database
phase has a 240-second lease and five-attempt limit. Function duration is set to
300 seconds. The SDK handler renews message leases while synchronous database/dbt
work runs in a thread. Expired database leases can be repaired and generation
fencing rejects old deliveries. `GET /_internal/outbox` only dispatches pending
wakeups, requires the exact server secret, and is scheduled daily under the free
plan. An extended Queue outage can therefore leave accepted work pending until
traffic, a successful consumer or the daily repair dispatch resumes. It is not a
promise of immediate processing during an outage.

The existing Airflow DAG and polling CLI remain available for separately managed
local/Linux orchestration. No Airflow scheduler, always-on worker or Vercel
Daemon is started by this deployment. Native Queue phases provide the hosted
finite orchestration path; do not label it hosted Airflow.

## Hosted verification before promotion

Verify the exact alias and deployed source; readiness must query Neon. Exercise
real signup/code verification, logout/reset and no-membership denial. Register
only the real observed identity, then check viewer/operator and cross-tenant
boundaries. Post an event with a private production source key and observe the
private Queue delivery, materialized receipt and duplicate replay. Run one real
queued daily success and one controlled dbt failure, compare the last-good
manifest hash, and exercise inventory mismatch. Confirm immutable objects survive
another function instance, the protected repair route denies unauthenticated
requests, function bundles contain no local environments/reports/secrets, and no
owner URL is configured. Preserve old local/Linux/benchmark evidence as history.

## Executed hosted checks, 2026-11-23

On the actual production alias, all 14 frozen input lines were accepted/replayed,
with 11 distinct events across two tenants. Authentication, duplicate acceptance,
identity conflict, invalid payload and cross-tenant source denial were checked.
The private Queue materialized the expected receipts, revisions and quarantines.

After fixing a deployment ignore rule and the dbt semaphore compatibility, a
fresh good daily job completed stage/check/publish, with 13 actual dbt tests
passing. A new internally consistent but incorrect oracle then failed actual
dbt reconciliation tests. The successful publication's manifest pointer stayed
exactly unchanged. Inventory processing completed while retaining a failed
quality result: 20 observed units versus 22 independently referenced units,
difference -2. Nineteen immutable objects across the five retained attempts were
checksum-verified. The two earlier unexpected failures remain in the job ledger.
These jobs used reviewed restricted-app internal preconditions followed by the
deployed private Queue; they are separate from an HTTP operator job-submission
test. Historical fixture dates correctly remain stale.

A real browser journey used the enrolled verified operator account, actual Neon
JWTs and all seven workbench tabs without network mocks. All 28 checks passed,
including a known Beta source's 404 denial, empty browser storage, logout and
anonymous API denial. Credentials, cookies and tokens were kept out of reports.

An actual Neon PostgreSQL 18.7 exported snapshot was restored into a newly
created ownership-marked database on the same project. All 25 application-table
hashes matched, all 23 forced-RLS tables were restored, restricted unscoped reads
returned no tenants, and scoped reads returned only their own tenant. All 19
stored objects matched their hashes and the restored immutability trigger
rejected mutation. Source connections were read-only; the temporary database
and private dump were removed. Managed authentication, warehouse and evaluation
schemas were excluded. This does not establish provider PITR, complete account
recovery, cross-region failover or a recovery-time guarantee.
