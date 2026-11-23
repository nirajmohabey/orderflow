{{ config(alias=candidate_alias('expectations')) }}
select order_total_minor, order_count, currency
from orderflow.pipeline_expectations
where tenant_id='{{ checked_uuid(var("tenant_id")) }}'::uuid
  and run_id='{{ checked_uuid(var("run_id")) }}'::uuid
