select f.entity_id from {{ ref('order_facts') }} f
left join {{ ref('stg_revisions') }} r on r.tenant_id=f.tenant_id and r.entity_type='order'
  and r.entity_id=f.entity_id and r.entity_version=f.entity_version and r.event_id=f.event_id
where r.entity_id is null or f.total_minor<0
