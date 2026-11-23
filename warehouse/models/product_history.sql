{{ config(alias=candidate_alias('product_history')) }}
with equal_time as (
  select *,row_number() over(partition by tenant_id,entity_id,effective_at order by entity_version desc) as time_rank
  from {{ ref('stg_revisions') }} where entity_type='product'
), selected as (
  select * from equal_time where time_rank=1
)
select tenant_id,entity_id,entity_version,source_id,event_id,effective_at as valid_from,
       lead(effective_at) over(partition by tenant_id,entity_id order by effective_at) as valid_to,
       payload->>'sku' as sku,payload->>'name' as name,
       (payload->>'unit_price_minor')::bigint as unit_price_minor,payload->>'currency' as currency
from selected
