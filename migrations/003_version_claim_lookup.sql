-- Preserve the first durably accepted authoritative payload/effective-time
-- meaning, including quarantined envelopes, without scanning an entity's
-- unbounded inbox history. Existing migrations and history remain immutable.
CREATE INDEX inbox_version_claim_idx ON orderflow.inbox_events
    (tenant_id,source_id,entity_type,entity_id,entity_version,inbox_sequence);
