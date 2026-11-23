with actual as(select count(*) as orders,coalesce(sum(total_minor),0) as total from {{ ref('order_facts') }})
select actual.* from actual cross join {{ ref('expectations') }} expected
where actual.orders<>expected.order_count or actual.total<>expected.order_total_minor
