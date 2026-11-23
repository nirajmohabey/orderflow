select tenant_id,entity_type,entity_id,entity_version,count(*) from {{ ref('stg_revisions') }}
group by 1,2,3,4 having count(*)<>1
