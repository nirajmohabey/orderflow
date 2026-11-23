# OrderFlow pipeline integration contract

Schema `orderflow`, runtime `orderflow_app`; transformation login `orderflow_transform` owns only `orderflow_warehouse`. Pipeline owns `connectors.py`, `objects.py`, `pipeline.py`, migration002, warehouse, orchestration and its tests/scripts. Core exposes the protected GET routes and supplies `db.tenant_transaction`.

## Readers

`GET /v1/reports/latest` calls `pipeline.read_latest_report(tenant_id)`. It returns:

```json
{"has_success":false,"stale":true,"observed_at":"UTC",
 "last_good":null,"latest_attempt":null}
```

When available, `last_good` is `{run_id,source_id,interval_start,interval_end,published_at,manifest_sha256,storage:"local",artifact_availability:"local_only",summary:{order_count,order_total_minor,currency,product_count,raw_revision_count},order_facts:[{entity_id,entity_version,total_minor,currency,status}],product_history:[{entity_id,entity_version,valid_from,valid_to,unit_price_minor,currency}],preview_limit:100,preview_truncated:boolean,manifest:{...}}`. Facts/history are bounded previews; the manifest states complete row counts and hashes. `latest_attempt` is `{run_id,status,interval_start,interval_end,started_at,finished_at,failure_code,checks:[{name,status,observed,expected}]}`. A failed later attempt does not replace last_good. No successful publication is explicit, never a healthy zero. Old fixed fixture intervals can be stale by actual clock age.

`GET /v1/inventory/latest?source_id=<optionalUUID>` calls `pipeline.read_inventory_latest(tenant_id,source_id=None)` and returns `{has_snapshot,latest:null|InventoryReport}`.

`GET /v1/inventory/history?source_id=<optionalUUID>&limit=20` calls `pipeline.read_inventory_history(tenant_id,source_id=None,limit=20)` and returns `{items:[InventoryReport],truncated:boolean}`; limit 1–20. This is a bounded recent-history endpoint, not a full-export pagination claim.

`InventoryReport` is `{run_id,source_id,snapshot_id,reference_id,as_of,status:"pass"|"fail",observed_sha256,reference_sha256,observed_total,reference_total,difference_total,differences:[{sku,observed_quantity,reference_quantity,difference,observed_present,reference_present}],manifest_sha256,manifest:{...},storage:"local",artifact_availability:"local_only"}`. The original supplied inventory fixture reports 20 versus 22, difference −2 and fail. Missing SKU sides have presence=false and arithmetic quantity 0; this does not claim a missing observation was measured as zero. CSV inputs never modify product/order entities. Inventory differences remain inspectable after failed checks.

Amounts/versions are JSON integers. Browser code must not round values beyond its safe range. Raw credentials and machine file paths are not API response fields. `local_only` means no hosted artifact-download claim; bounded report JSON is persisted in PostgreSQL for cross-host readers. Both readers enforce tenant context through the actual runtime role. A supplied source UUID with no scoped inventory simply has no snapshot.

## Batch and connector semantics

Daily runs have an explicitly configured existing source as reporting owner, retaining the core run API's source UUID. Manifests name all participating authoritative sources; ownership is not falsely presented as a single-source lineage. Immutable per-run snapshot and independent expected-order rows feed real dbt models. dbt run and tests must succeed before a monotonic last-good pointer transaction. Same-interval identical source/oracle/project fingerprints reuse the current publication; older candidates cannot replace newer generations.

Catalog fetches real paginated HTTP and posts every item to the actual source-scoped HTTP endpoint. Only after every item receives durable 202 does a separate cursor-CAS transaction verify all inbox identities and advance that page. Partial/uncertain acceptance leaves the old cursor and stable identities replay safely. There are no source keys in URLs/logs/XCom. The simulator is a separately launched loopback HTTP service, explicitly synthetic.

Root creates three isolated environments: app `.venv`, dbt `.venv-dbt`, Airflow `.venv-airflow`. dbt pins core 1.12.7/postgres 1.11.0; Airflow pins 3.4.0 and the official Python 3.12 constraints. DAG tasks invoke the app Python subprocess and dbt uses its own executable. No Airflow/dbt installation in the API dependency environment.
