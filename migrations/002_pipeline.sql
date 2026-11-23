-- Additive pipeline state. Source snapshots and reports never overwrite evidence.
CREATE TABLE orderflow.report_locks (
    tenant_id uuid NOT NULL REFERENCES orderflow.tenants(tenant_id),
    report_key text NOT NULL CHECK(report_key='daily'),
    PRIMARY KEY(tenant_id,report_key)
);
CREATE TABLE orderflow.report_candidates (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    generation bigint GENERATED ALWAYS AS IDENTITY,
    report_key text NOT NULL DEFAULT 'daily' CHECK(report_key='daily'),
    status text NOT NULL CHECK(status IN ('staging','checked','published','superseded','failed')),
    snapshot_sha256 char(64),
    oracle_sha256 char(64) NOT NULL CHECK(oracle_sha256 ~ '^[a-f0-9]{64}$'),
    project_sha256 char(64) NOT NULL CHECK(project_sha256 ~ '^[a-f0-9]{64}$'),
    manifest_sha256 char(64),
    manifest jsonb,
    report_payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,run_id),
    UNIQUE(tenant_id,run_id,generation),
    FOREIGN KEY(tenant_id,run_id) REFERENCES orderflow.ingestion_runs(tenant_id,run_id),
    CHECK(manifest IS NULL OR octet_length(manifest::text)<=262144),
    CHECK(report_payload IS NULL OR octet_length(report_payload::text)<=524288),
    CHECK(status IN ('staging','failed') OR
      (snapshot_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL AND manifest IS NOT NULL AND report_payload IS NOT NULL))
);
CREATE TABLE orderflow.pipeline_snapshot_rows (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    entity_type text NOT NULL CHECK(entity_type IN ('product','order')),
    entity_id text NOT NULL,
    entity_version bigint NOT NULL,
    source_id uuid NOT NULL,
    event_id uuid NOT NULL,
    effective_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY(tenant_id,run_id,entity_type,entity_id,entity_version),
    FOREIGN KEY(tenant_id,run_id) REFERENCES orderflow.ingestion_runs(tenant_id,run_id),
    CHECK(octet_length(payload::text)<=32768)
);
CREATE TABLE orderflow.pipeline_expectations (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    order_total_minor bigint NOT NULL CHECK(order_total_minor>=0),
    order_count bigint NOT NULL CHECK(order_count>=0),
    currency char(3) NOT NULL,
    PRIMARY KEY(tenant_id,run_id),
    FOREIGN KEY(tenant_id,run_id) REFERENCES orderflow.ingestion_runs(tenant_id,run_id)
);
CREATE TABLE orderflow.pipeline_expected_orders (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    entity_id text NOT NULL,
    entity_version bigint NOT NULL,
    total_minor bigint NOT NULL,
    currency char(3) NOT NULL,
    status text NOT NULL,
    PRIMARY KEY(tenant_id,run_id,entity_id),
    FOREIGN KEY(tenant_id,run_id) REFERENCES orderflow.pipeline_expectations(tenant_id,run_id)
);
CREATE TABLE orderflow.report_publications (
    tenant_id uuid NOT NULL,
    report_key text NOT NULL CHECK(report_key='daily'),
    run_id uuid NOT NULL,
    generation bigint NOT NULL,
    interval_start timestamptz NOT NULL,
    interval_end timestamptz NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    manifest_sha256 char(64) NOT NULL,
    PRIMARY KEY(tenant_id,report_key),
    FOREIGN KEY(tenant_id,run_id,generation) REFERENCES orderflow.report_candidates(tenant_id,run_id,generation),
    CHECK(interval_end>interval_start)
);
CREATE TABLE orderflow.inventory_snapshots (
    tenant_id uuid NOT NULL,
    source_id uuid NOT NULL,
    snapshot_id text NOT NULL,
    reference_id text NOT NULL,
    run_id uuid NOT NULL,
    as_of timestamptz NOT NULL,
    observed_sha256 char(64) NOT NULL,
    reference_sha256 char(64) NOT NULL,
    manifest_sha256 char(64) NOT NULL,
    status text NOT NULL CHECK(status IN ('pass','fail')),
    report jsonb NOT NULL CHECK(octet_length(report::text)<=1048576),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,source_id,snapshot_id),
    FOREIGN KEY(tenant_id,source_id) REFERENCES orderflow.sources(tenant_id,source_id),
    FOREIGN KEY(tenant_id,run_id) REFERENCES orderflow.ingestion_runs(tenant_id,run_id)
);

CREATE FUNCTION orderflow.guard_pipeline_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable_pipeline_evidence' USING ERRCODE='23514'; END $$;
CREATE TRIGGER immutable_snapshot BEFORE UPDATE OR DELETE ON orderflow.pipeline_snapshot_rows FOR EACH ROW EXECUTE FUNCTION orderflow.guard_pipeline_evidence();
CREATE TRIGGER immutable_expectations BEFORE UPDATE OR DELETE ON orderflow.pipeline_expectations FOR EACH ROW EXECUTE FUNCTION orderflow.guard_pipeline_evidence();
CREATE TRIGGER immutable_expected_orders BEFORE UPDATE OR DELETE ON orderflow.pipeline_expected_orders FOR EACH ROW EXECUTE FUNCTION orderflow.guard_pipeline_evidence();
CREATE TRIGGER immutable_inventory BEFORE UPDATE OR DELETE ON orderflow.inventory_snapshots FOR EACH ROW EXECUTE FUNCTION orderflow.guard_pipeline_evidence();
CREATE FUNCTION orderflow.guard_snapshot_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE candidate_state text; frozen_hash text;
BEGIN
  SELECT c.status,c.snapshot_sha256 INTO candidate_state,frozen_hash FROM orderflow.report_candidates c
    WHERE c.tenant_id=NEW.tenant_id AND c.run_id=NEW.run_id FOR SHARE;
  IF candidate_state IS DISTINCT FROM 'staging' OR frozen_hash IS NOT NULL THEN
    RAISE EXCEPTION 'frozen_pipeline_snapshot' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER snapshot_insert_gate BEFORE INSERT ON orderflow.pipeline_snapshot_rows FOR EACH ROW EXECUTE FUNCTION orderflow.guard_snapshot_insert();
CREATE TRIGGER expected_insert_gate BEFORE INSERT ON orderflow.pipeline_expectations FOR EACH ROW EXECUTE FUNCTION orderflow.guard_snapshot_insert();
CREATE TRIGGER expected_order_insert_gate BEFORE INSERT ON orderflow.pipeline_expected_orders FOR EACH ROW EXECUTE FUNCTION orderflow.guard_snapshot_insert();

CREATE FUNCTION orderflow.guard_report_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR OLD.status IN ('published','superseded','failed') THEN
    RAISE EXCEPTION 'immutable_report_candidate' USING ERRCODE='23514';
  END IF;
  IF OLD.status='checked' AND ((to_jsonb(OLD)-'status') IS DISTINCT FROM (to_jsonb(NEW)-'status')
      OR NEW.status NOT IN ('published','superseded','failed')) THEN
    RAISE EXCEPTION 'immutable_checked_candidate' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_candidate BEFORE UPDATE OR DELETE ON orderflow.report_candidates FOR EACH ROW EXECUTE FUNCTION orderflow.guard_report_candidate();
CREATE FUNCTION orderflow.guard_report_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE candidate orderflow.report_candidates%ROWTYPE; batch orderflow.ingestion_runs%ROWTYPE;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retain_last_good_report' USING ERRCODE='23514'; END IF;
  SELECT * INTO candidate FROM orderflow.report_candidates c WHERE c.tenant_id=NEW.tenant_id AND c.run_id=NEW.run_id FOR SHARE;
  SELECT * INTO batch FROM orderflow.ingestion_runs r WHERE r.tenant_id=NEW.tenant_id AND r.run_id=NEW.run_id;
  IF candidate.status IS DISTINCT FROM 'checked' OR NEW.manifest_sha256 IS DISTINCT FROM candidate.manifest_sha256
      OR NEW.interval_start IS DISTINCT FROM batch.interval_start OR NEW.interval_end IS DISTINCT FROM batch.interval_end THEN
    RAISE EXCEPTION 'unchecked_report_publication' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND (NEW.interval_end,NEW.generation)<=(OLD.interval_end,OLD.generation) THEN
    RAISE EXCEPTION 'report_publication_regression' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER monotonic_report_pointer BEFORE INSERT OR UPDATE OR DELETE ON orderflow.report_publications FOR EACH ROW EXECUTE FUNCTION orderflow.guard_report_pointer();

DO $$
DECLARE relation text;
BEGIN
  FOREACH relation IN ARRAY ARRAY['report_locks','report_candidates','pipeline_snapshot_rows','pipeline_expectations',
      'pipeline_expected_orders','report_publications','inventory_snapshots'] LOOP
    EXECUTE format('ALTER TABLE orderflow.%I ENABLE ROW LEVEL SECURITY',relation);
    EXECUTE format('ALTER TABLE orderflow.%I FORCE ROW LEVEL SECURITY',relation);
    EXECUTE format('CREATE POLICY tenant_scope ON orderflow.%I USING (tenant_id=nullif(current_setting(''orderflow.tenant_id'',true),'''')::uuid) WITH CHECK (tenant_id=nullif(current_setting(''orderflow.tenant_id'',true),'''')::uuid)',relation);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE ON orderflow.%I TO orderflow_app',relation);
  END LOOP;
END $$;
GRANT USAGE,SELECT ON SEQUENCE orderflow.report_candidates_generation_seq TO orderflow_app;
GRANT USAGE ON SCHEMA orderflow TO orderflow_transform;
GRANT SELECT ON orderflow.pipeline_snapshot_rows,orderflow.pipeline_expectations,orderflow.pipeline_expected_orders TO orderflow_transform;
