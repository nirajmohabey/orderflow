with ordered as(
 select *,lead(valid_from) over(partition by tenant_id,entity_id order by valid_from) as next_start
 from {{ ref('product_history') }}
)
select * from ordered where valid_to<=valid_from
  or (next_start is not null and valid_to is distinct from next_start)
  or (next_start is null and valid_to is not null)
