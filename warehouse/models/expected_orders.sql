{{ config(alias=candidate_alias('expected_orders')) }}
select entity_id, entity_version, total_minor, currency, status
from orderflow.pipeline_expected_orders
where tenant_id='{{ checked_uuid(var("tenant_id")) }}'::uuid
  and run_id='{{ checked_uuid(var("run_id")) }}'::uuid
