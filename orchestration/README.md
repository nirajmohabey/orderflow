# Finite daily batch execution

The API, continuous partition worker, Airflow and dbt have different responsibilities. Airflow schedules one fixed UTC data interval; each target runs snapshot/export, actual dbt models/tests, then publication. The worker is not an Airflow task. No network/model call occurs while a worker partition is locked.

Use separate Python 3.12 environments. The app's locked environment is created by the root setup. On Linux:

```sh
python3.12 -m venv .venv-airflow
.venv-airflow/bin/pip install -r orchestration/requirements-airflow.in --constraint orchestration/constraints-hardened-3.12.txt
python3.12 -m venv .venv-dbt
.venv-dbt/bin/pip install --require-hashes -r warehouse/requirements-dbt.lock
.venv-dbt/bin/pip check
```

The original official Airflow 3.4.0/Python 3.12 constraints file is preserved unchanged, SHA256 `3cecb4fe4a11d69bd772aef18434825bbed9b344703302ca0a55d50e30a33eda`. The reviewed hardened copy, SHA256 `5001dcab2e85da66f9ac2139e7bcffc7b3b736e3b37c29f256544cc13837c9a2`, changes only dnspython 2.9.1 to 2.9.2 for [the upstream WKS parser advisory](https://github.com/rthalley/dnspython/security/advisories/GHSA-vvf4-9x63-8xvv). Install the selected base Airflow/psycopg environment, not unused optional provider extras. Official all-extras constraints contain additional advisory matches for packages absent from the executed DAG environment; they remain explicitly reported in the security inventory. The constraint patch requires its own pip check, bounded parser regression and actual finite DAG result; inspect `artifacts/security/airflow-constraint-hardening.json` for current evidence status. Official PyPI metadata confirms dbt-core 1.12.7 and dbt-postgres 1.11.0 have compatible adapter requirements and support Python 3.12. [Airflow installation](https://airflow.apache.org/docs/apache-airflow/3.4.0/installation/installing-from-pypi.html), [dbt-core 1.12.7](https://pypi.org/project/dbt-core/1.12.7/), [dbt-postgres 1.11.0](https://pypi.org/project/dbt-postgres/1.11.0/).

After the complete literal fixture stream is accepted and workers have drained, run a day containing all effective times:

```sh
export ODF_DBT_EXECUTABLE="$PWD/.venv-dbt/bin/dbt"
.venv/bin/python scripts/pipeline.py daily \
  --tenant 11111111-1111-4111-8111-111111111111 \
  --source bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb \
  --start 2026-11-06T00:00:00Z --end 2026-11-07T00:00:00Z \
  --oracle fixtures/batch_oracle.json
```

`ODF_DBT_DATABASE_URL` is a separate least-privilege transformation login that owns only `orderflow_warehouse` and can select the three run-scoped source/expectation relations. Bootstrap provisions it outside source. The subprocess sets `PGOPTIONS` with the validated tenant UUID and statement/lock timeouts. Never put database passwords or source keys in command arguments or committed artifacts. Each model relation has a run-UUID suffix, so concurrent attempts do not overwrite another run's tables.

For a real finite Airflow run, configure the already provisioned PostgreSQL metadata database using its secret DSN in `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`, then:

```sh
export ODF_PROJECT_ROOT="$PWD"
export ODF_APP_PYTHON="$PWD/.venv/bin/python"
export ODF_BATCH_TARGETS='[{"tenant_id":"11111111-1111-4111-8111-111111111111","source_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}]'
export AIRFLOW_HOME="$PWD/.runtime/airflow"
.venv-airflow/bin/airflow db migrate
.venv-airflow/bin/airflow dags test orderflow_daily 2026-11-06T00:00:00+00:00 \
  --conf '{"interval_start":"2026-11-06T00:00:00Z","interval_end":"2026-11-07T00:00:00Z"}' \
  -f orchestration/dags/orderflow_daily.py
```

The DAG fails when targets are unconfigured. It emits only bounded run IDs/status to XCom. Scheduled `data_interval_start/end` drive every stage; machine now does not choose the data interval. Airflow 3 manual `dags test` supplies a zero-length timetable interval, so manual runs must provide both explicit bounds in DagRun conf as above; the app still validates an exact 24-hour UTC day. The source snapshot includes committed materialized revisions before the interval end, including late arrivals already materialized when that snapshot is captured. Its single `INSERT SELECT` fixes a consistent database snapshot; later arrivals require another candidate/rebuild. The reporting-owner source is explicitly labeled, and every participating authority is listed in the manifest.

For ingestion in the same finite DAG, add `catalog_source_id`, `catalog_url`, `api_base`, `order_source_id` and `order_event_file` to the reviewed target JSON. The `ingest` task calls the actual HTTP adapters, records source-scoped ingestion runs and durable-acceptance checks, then performs a bounded worker drain before snapshotting. Omitting connector fields explicitly selects a pre-ingested batch. A completed catalog cursor is a no-op by default; CLI `catalog --refresh` begins another checked dataset cycle, and the DAG uses that option. During an unfinished cycle a changed dataset hash fails instead of silently mixing pages. Source keys are read from the private `.runtime/source-keys.json`, never target JSON or XCom.

Expected failure demonstration: after one successful publication, rerun `daily` with `--test-corrupt-total` in an explicitly local environment. The actual dbt reconciliation must fail and the prior last-good pointer/hash/time must survive. `ODF_TEST_CORRUPT_TOTAL=true` exercises the same test-only failure through the DAG. No remote API enables these fault flags. Other local barriers include after raw export, staged manifest, dbt run/test and before/after pointer commit. Missing/corrupt object checks fail admission.

Raw exports and report manifests are immutable local objects, with original CSV bytes and both hashes retained. `storage:local` and `artifact_availability:local_only` are explicit in the API. An injected-client S3 adapter implements conditional writes and hash reads for contract testing; no AWS account, actual S3 execution or shared-filesystem cloud artifact download is claimed. Durable report JSON and the last-good pointer live in PostgreSQL so a hosted API can display results without mounting the batch runner's disk.

Inventory is independent: the original 20-observed/22-reference fixture fails with difference −2. It never changes order/product data. A separately labeled matched control demonstrates a passing inventory run; the original files remain unchanged. Quality failures remain visible instead of becoming healthy zeroes.

## Executed evidence

`artifacts/pipeline-connectors-tests.xml` records 30 passing native regressions with no skips. These include actual loopback HTTP, hard process exit and commit-uncertain cursor recovery, source-run tracking, independent CSV differences, frozen snapshots, actual dbt reconciliation, idempotent and delayed publication, and tenant isolation. `artifacts/pipeline/dbt-execution-evidence.json` records 13 successful dbt checks; intentionally corrupting the total fails `order_reconciliation` and `total_reconciliation`. `serving-publication-proof.json` verifies that the prior last-good payload/hash/time survives and the original inventory report remains 20 versus 22, difference −2. These are synthetic local measurements, not hosted throughput, cloud-storage durability or an unattended-production guarantee.

The S3 adapter follows the [official conditional PutObject contract](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html): an existing key returns 412; a conflicting operation can return 409 and fails for a caller retry. Tests use an injected fake client; actual S3 execution remains unavailable. API reader shapes are documented in `API.md`.
