{{ config(alias=candidate_alias('order_facts')) }}
with revisions as (
  select *,row_number() over(partition by tenant_id,entity_id order by entity_version desc) as version_rank
  from {{ ref('stg_revisions') }} where entity_type='order'
)
select tenant_id,entity_id,entity_version,source_id,event_id,effective_at,
       (payload->>'total_minor')::bigint {% if var('test_corrupt_total', false) %}+1{% endif %} as total_minor,
       payload->>'currency' as currency,payload->>'status' as status
from revisions where version_rank=1
