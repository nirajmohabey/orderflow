select count(*) as invalid_count from {{ ref('expectations') }} having count(*)<>1
