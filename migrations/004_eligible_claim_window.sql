-- An ordered eligible prefix avoids recomputing/aggregating every backlogged
-- partition for each single-event transaction. Historical terminal rows do not
-- occupy this index; one claim considers at most128 enabled candidate events.
CREATE INDEX inbox_eligible_window_idx ON orderflow.inbox_events
    (tenant_id,available_at,inbox_sequence) INCLUDE(source_id,partition_key)
    WHERE status IN('pending','retry_wait');
