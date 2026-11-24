# Hosting and account boundaries

## Current Vercel/Neon deployment

[OrderFlow is deployed](https://orderflow-hazel.vercel.app) on the existing Vercel Hobby/Neon free-plan setup. FastAPI, the compiled React workbench and private finite Queue consumers use Neon PostgreSQL and Managed Better Auth. [PRODUCTION.md](PRODUCTION.md) records configuration, enrollment, actual hosted ingestion/publication/failure/inventory evidence, bounded recovery and operating limits. The [customer mapping exercise](CUSTOMER_MAPPING.md) adds a synthetic example around the existing connector; it creates no cloud service or hosted endpoint.

Builds compile the dashboard and bundle the existing application. They do not migrate, seed or start an always-running polling worker. GitHub pushes to `main` automatically deploy through the approved connection. A successful build alone is insufficient: verify the stable alias's source, readiness, exact frontend assets, authentication and application boundaries separately.

## Identity and database access

Production requires `ODF_APP_ENV=production`, `ODF_DEMO_MODE=false`, exact HTTPS origins/hosts and reviewed server-maintained memberships. Neon JWT verification uses Ed25519 signatures, the fixed trusted issuer/audience and time bounds. Signup alone grants no workspace authority; fixture sign-in remains local-only. A separately configured HS256/RS256 bearer profile is distinct from the hosted Neon experience.

Use a reviewed owner connection outside the HTTP project's environment for migrations and enrollment. The deployed project contains restricted `orderflow_app` and `orderflow_transform` connections, never a migration/administrative owner URL. The private Queue needs the transform connection for finite dbt phases; Vercel functions share project environment variables. These are separate least-privilege database roles within one deployment secret boundary, not per-function secret isolation. Separate development and production databases and keep source keys out of the browser.

## Processing and objects

The hosted path uses native private Vercel Queues, transactional outbox records, bounded leases/retries and finite drain/stage/check/publish phases. A daily Hobby cron repairs outbox dispatch; it is not continuous polling or an immediate outage-recovery guarantee. The local polling worker and Linux Airflow DAG remain separate execution profiles. No always-on Vercel daemon or hosted Airflow scheduler is claimed or required for this deployment.

Hosted immutable source/report objects live in tenant-scoped PostgreSQL bytea records, with 8 MiB object limits and SHA-256 verification. Authenticated APIs return their authorized report data. They consume Neon storage; no automatic retention purge is implied. The local filesystem adapter and injected-client S3 contract tests retain their separate scope; no AWS account or real S3 operation is claimed.

## Recovery and free-plan limits

An actual Neon application-schema restore matched all 25 table hashes and checked 23 forced-RLS tables under restricted roles. It excluded managed authentication and the warehouse schema; see the exact exclusions in PRODUCTION. This does not establish full-account recovery, scheduled external backups, cross-region failover or an RTO/SLA.

Watch existing Vercel/Neon storage, execution and email quotas, job failures and connection budgets. Keep retries bounded and retain failed attempts/last-good publications. Do not upgrade plans or create another provider simply to reproduce this example. Free quotas and cold starts remain limits; the separate Linux core benchmark does not measure managed production capacity.
