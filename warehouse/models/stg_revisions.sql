{{ config(alias=candidate_alias('stg_revisions')) }}
select tenant_id, entity_type, entity_id, entity_version, source_id, event_id, effective_at, payload
from orderflow.pipeline_snapshot_rows
where tenant_id='{{ checked_uuid(var("tenant_id")) }}'::uuid
  and run_id='{{ checked_uuid(var("run_id")) }}'::uuid
