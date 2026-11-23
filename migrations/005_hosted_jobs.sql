-- Persistent hosted artifacts and a transactionally enqueued, fenced job outbox.
CREATE TABLE orderflow.stored_objects (
    tenant_id uuid NOT NULL REFERENCES orderflow.tenants(tenant_id),
    object_key text NOT NULL CHECK(length(object_key) BETWEEN 1 AND 1000),
    sha256 char(64) NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    content bytea NOT NULL CHECK(octet_length(content)<=8388608),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,object_key),
    CHECK(encode(pg_catalog.sha256(content),'hex')=sha256)
);
CREATE TRIGGER immutable_stored_object BEFORE UPDATE OR DELETE ON orderflow.stored_objects
FOR EACH ROW EXECUTE FUNCTION orderflow.immutable_record();
CREATE TABLE orderflow.hosted_jobs (
    tenant_id uuid NOT NULL REFERENCES orderflow.tenants(tenant_id),
    job_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('drain','daily','inventory')),
    phase text NOT NULL DEFAULT 'start' CHECK(phase IN ('start','check','publish','done')),
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=262144),
    payload_sha256 char(64) NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','succeeded','failed')),
    generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid, lease_until timestamptz,
    dispatch_token uuid, dispatch_until timestamptz,
    published_generation bigint, published_at timestamptz,
    result jsonb CHECK(result IS NULL OR octet_length(result::text)<=262144),
    last_error_code text CHECK(last_error_code IS NULL OR length(last_error_code)<=100),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,job_id),
    CHECK((status='running')=(lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX hosted_jobs_pending ON orderflow.hosted_jobs(tenant_id,available_at) WHERE status='pending';
ALTER TABLE orderflow.stored_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE orderflow.stored_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON orderflow.stored_objects
USING(tenant_id=nullif(current_setting('orderflow.tenant_id',true),'')::uuid)
WITH CHECK(tenant_id=nullif(current_setting('orderflow.tenant_id',true),'')::uuid);
ALTER TABLE orderflow.hosted_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orderflow.hosted_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON orderflow.hosted_jobs
USING(tenant_id=nullif(current_setting('orderflow.tenant_id',true),'')::uuid)
WITH CHECK(tenant_id=nullif(current_setting('orderflow.tenant_id',true),'')::uuid);
REVOKE ALL ON orderflow.stored_objects,orderflow.hosted_jobs FROM PUBLIC;
GRANT SELECT,INSERT ON orderflow.stored_objects TO orderflow_app;
GRANT SELECT,INSERT ON orderflow.hosted_jobs TO orderflow_app;
GRANT UPDATE(phase,status,generation,attempts,available_at,lease_token,lease_until,dispatch_token,dispatch_until,published_generation,published_at,result,last_error_code,updated_at) ON orderflow.hosted_jobs TO orderflow_app;
