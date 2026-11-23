# Backend boundaries and operating contract

OrderFlow accepts synthetic source events into a durable PostgreSQL inbox, then processes one event per partition transaction. A 202 response means the inbox acceptance transaction committed. It does not mean the worker, an export or a quality check has completed. HTTP clients retry a commit-uncertain request with the same stable source identity. There is no exactly-once transport or remote-merchant guarantee.

## Identity and authorization

An accepted identity is `(tenant_id, source_id, source_event_id)`. The API verifies an opaque source key before accepting its bound source/tenant. The database stores SHA-256 hashes of high-entropy generated keys; the ignored local credential registry contains the usable development keys and must be protected separately. Source keys are not viewer/operator sessions.

Viewer/operator access requires a signature-verified JWT with the configured algorithm, issuer, audience, subject, expiry and issued-at claims. The server resolves the verified issuer/subject and requested tenant through active database memberships. A token-supplied role or tenant does not grant membership. Revoking a membership or source key affects subsequent authentication. In-flight authenticated work may already be committing; revocation is not represented as retroactive cancellation.

The fixture sign-in endpoint is enabled only when both `ODF_APP_ENV=local` and `ODF_DEMO_MODE=true`, using fixed reviewed aliases. Production exposes no fixture identities or signing endpoint. HS256 deployments require a separately managed trusted signer/shared secret; optional RS256 verifies a configured public key. These capabilities are not evidence that a real external identity provider was connected.

`orderflow_app` is a nonowner role without SUPERUSER, BYPASSRLS, CREATEDB, CREATEROLE or replication privileges. Every tenant transaction sets a local tenant context; forced RLS and composite foreign keys defend missing filters and crossed relationships. The current-entity view uses security-invoker semantics. Connection reuse cannot retain transaction-local tenant settings. Credential/membership registries have no runtime table grants; small fixed-search-path security-definer lookup functions return only the verified context. Administrative migration/seed credentials are separate.

RLS complements trusted server authorization. It is not claimed to protect every tenant from a stolen application database credential or a compromised server that deliberately selects another tenant context. There is no arbitrary SQL endpoint.

## Canonical event data

The strict envelope and product/order payloads reject extra fields, duplicate JSON keys, nonfinite numbers, invalid UTF-8, NUL/nonblank violations and nesting beyond 16 levels. Versions are strict integers from 1 through signed-bigint maximum; monetary values are nonnegative strict integers up to 10^15 minor units. JSON boolean/fractional values cannot stand in for integers. Individual money values fit JavaScript's safe integer range; aggregate totals and large versions require lossless frontend handling.

Timestamps require an explicit RFC3339 timezone, at most six fractional digits and a supported UTC year 2000–2100. Canonical bytes use sorted compact UTF-8 JSON, normalized UUIDs and UTC timestamps with six fractional digits. Payload and semantic-envelope hashes are separate. Server UUIDs, ingestion time, processing status and retry metadata do not affect semantic identity. Identifier case and Unicode content remain exact.

For a short entity, the stable partition is `type:id`; if that representation exceeds 128 UTF-8 bytes, the deterministic form is `type:sha256:<entity-id-digest>`. Identifiers are never truncated. A strict-shaped existing identity compares its canonical hash first: an identical replay returns its original UUID and current status, while changed semantic content returns 409. A new identity with a wrong derived partition returns422. This ordering intentionally distinguishes a changed existing identity from a newly invalid partition.

## Atomic processing and recovery

Each worker claims one eligible partition row with `FOR UPDATE SKIP LOCKED`, then one eligible inbox row ordered by its ingestion sequence. Other unlocked partitions can progress. No HTTP or model call occurs while those database locks are held.

Claim selection uses an ordered partial-index window of up to128 enabled candidate events and point partition locks, avoiding an aggregate/sort over the whole active backlog. If every candidate is locked, the worker backs off; later entries wait for prefix progress. The saved EXPLAIN comparison is query-level evidence, not a full-app speedup or a starvation guarantee.

In one transaction the worker checks for a receipt, verifies tenant currency and source authority, checks immutable revision identity and the nearest lower/upper effective-time neighbors, inserts a revision or determines a no-op, advances current only for a strictly higher version, writes one terminal receipt, finalizes inbox status and increments the partition checkpoint. A checkpoint records completed distinct identities; it is not a maximum timestamp filter. A valid late revision remains in history even when it cannot replace current.

Same-version identical content/effective time under a distinct event identity gets its own `duplicate_revision` receipt without a second revision. Changed content or effective time under that version is quarantined. Structurally valid wrong currency or source authority is a business quarantine, rather than a misleading ingress422. A quarantined identity is terminal; correction requires a newly identified event and an appropriate source version.

New acceptance and processing share the entity partition lock: the first durably accepted authoritative envelope reserves its version's payload/effective-time meaning even while pending or quarantined, so a changed correction requires a new version; a nonauthoritative source cannot reserve that claim.

Database triggers forbid semantic inbox changes/deletion and revision/receipt/audit/check mutation, enforce monotonic current pointers and defer the inbox-terminal/receipt consistency check until commit. Composite foreign keys retain exact tenant/source/entity/revision lineage. The application transaction additionally binds checkpoints and effects; the independent acceptance observer checks their complete sets and counts.

A hard process exit before commit rolls all projection/receipt/checkpoint writes back together. An exit after commit leaves one durable receipt; restart cannot repeat the effect. The real process-death runner covers both sides of commit. This does not claim to prevent every hardware/storage failure or replace backups.

After a transient failure rolls back, a separate short transaction schedules retry, rechecking the current row/receipt under locks. It uses capped exponential backoff and a default maximum of five persisted attempts. Successful terminal processing increments the counter; a failed attempt is counted when its retry/terminal scheduling commits. Hard-crashed transactions and superseded failure schedulers do not provide an exact physical-start counter. The durable retry policy is not presented as telemetry of every process start.

An operator can expedite only `retry_wait` with no receipt, retaining attempts and supplying an 8–500-character reason. Scheduling and immutable operator audit commit together. A viewer cannot retry; completed/quarantined identities cannot be reopened. The worker receives an explicit tenant allowlist, rather than a cross-tenant BYPASSRLS role.

## Read models and freshness

Order facts are latest snapshots, not additive sales events. Product history selects the highest source version for each equal effective timestamp before deriving half-open intervals; all raw revisions remain. The protected query-form history endpoint handles opaque IDs containing slash or dot segments without browser path normalization.

Event/quarantine pages use signed, expiring tenant/filter-bound keyset cursors. Current-entity cursors reference an immutable revision UUID so a long Unicode identifier fits the cursor byte budget and later current updates do not invalidate the position. Paging is a moving traversal, not a cross-request snapshot: new rows above a prior position may require refreshing from the first page.

Overview `counts.pending` is pending-only and `counts.retry_wait` is separate. Backlog is their sum. A source's supplied `pending_count` includes both. Missing freshness/latency values remain `null`; an empty queue is not a measured zero latency. Pipeline report publication and last-good/stale behavior are implemented separately and must be verified with the actual Airflow/dbt artifacts.

## Request and deployment controls

Requests default to 64 KiB with a five-second body receipt timeout. Exact host/origin allowlists, bearer length limits, SQL statement/lock waits, bounded pages and PostgreSQL-backed authenticated request budgets apply. The canonical local browser origin is port 8010; a separate Vite development origin requires explicit configuration. Responses carry request IDs and restrictive browser headers. Logs avoid raw request bodies, credentials, customer identifiers and database exception text. These are bounded controls, not a measured DDoS/capacity guarantee.

Initial CLI/dev configuration may load the ignored `.env` file once. A process already supplied with database/auth environment keys validates its explicit environment without repeatedly parsing the file. File changes require a restart; configuration is not a live file-watching service.

Run the focused core suite with `python -m pytest tests/test_core.py tests/test_benchmark_oracle.py tests/test_version_contract.py`; it creates and removes fresh owned databases after verifying the dedicated test destinations. It never resets the serving inbox. The focused result is distinct from a full-suite release proof; use the source-bound runner in [CHECKS.md](CHECKS.md) for that gate and its actual count. The upstream Starlette/httpx deprecation warning remains visible. Independent acceptance, process-death, pipeline, browser and backup checks have their own artifacts and denominators.

The benchmark freezes synthetic inputs and an independent truth ledger before timing actual loopback HTTP and separate workers. It records observed committed-visibility latency, not an exact PostgreSQL commit timestamp: the stored `committed_at` default is a transaction marker. Polling/query overhead is disclosed. Prototype failures remain available, and a partial trial is not a completed one-versus-four-worker comparison. Refer to final series summaries for actual completed measurements; no throughput or cloud capacity is inferred in this document.
