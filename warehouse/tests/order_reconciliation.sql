select coalesce(actual.entity_id,expected.entity_id) as entity_id
from {{ ref('order_facts') }} actual
full join {{ ref('expected_orders') }} expected using(entity_id)
where actual.entity_id is null or expected.entity_id is null
   or (actual.entity_version,actual.total_minor,actual.currency,actual.status)
      is distinct from (expected.entity_version,expected.total_minor,expected.currency,expected.status)
