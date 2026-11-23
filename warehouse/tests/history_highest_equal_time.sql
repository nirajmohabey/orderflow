select h.entity_id,h.valid_from from {{ ref('product_history') }} h
where h.entity_version<>(select max(r.entity_version) from {{ ref('stg_revisions') }} r
  where r.tenant_id=h.tenant_id and r.entity_type='product' and r.entity_id=h.entity_id and r.effective_at=h.valid_from)
