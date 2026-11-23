export type Int = number | string;
export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type Principal = {
  tenant_id: string;
  subject: string;
  role: "operator" | "viewer";
};
export type Tenant = { tenant_id: string; name: string; currency: string };
export type Session = {
  access_token: string;
  principal: Principal;
  tenant: Tenant;
  demo?: boolean;
};
export type Config = {
  mode: string;
  fixture_login_enabled: boolean;
  identities: { id: string; label: string; tenant_id: string; role: string }[];
};
export type Page<T> = { items: T[]; next_cursor: string | null };
export type Source = {
  source_id: string;
  name: string;
  connector_type: string;
  enabled: boolean;
  authoritative_entity_types: string[];
  accepted_count: Int;
  completed_count: Int;
  pending_count: Int;
  retry_wait_count: Int;
  quarantine_count: Int;
  oldest_pending_at: string | null;
  last_commit_at: string | null;
  last_successful_interval_end: string | null;
};
export type Run = {
  run_id: string;
  source_id: string;
  status: string;
  interval_start: string;
  interval_end: string;
  started_at: string;
  finished_at: string | null;
  failure_code: string | null;
  checks?: Check[];
};
export type Check = {
  name: string;
  status: string;
  observed: Json;
  expected: Json;
};
export type Overview = {
  tenant: Tenant;
  observed_at: string;
  counts: Record<
    | "accepted"
    | "pending"
    | "retry_wait"
    | "completed"
    | "quarantined"
    | "revisions"
    | "products"
    | "orders",
    Int
  >;
  order_totals: {
    current_amount_minor: Int;
    paid_amount_minor: Int;
    currency: string;
  };
  freshness: {
    oldest_pending_at: string | null;
    last_commit_at: string | null;
    last_successful_interval_end: string | null;
  };
  sources: Source[];
  recent_runs: Run[];
};
export type Entity = {
  entity_id: string;
  entity_version: Int;
  source_id: string;
  event_id: string;
  effective_at: string;
  payload: {
    sku?: string;
    name?: string;
    unit_price_minor?: Int;
    customer_id?: string;
    total_minor?: Int;
    currency: string;
    status?: string;
  };
};
export type Revision = {
  entity_version: Int;
  effective_at?: string;
  valid_from?: string;
  valid_to?: string | null;
  event_id?: string;
  payload: Entity["payload"];
};
export type History = {
  entity_id: string;
  raw_revisions: Revision[];
  intervals: Revision[];
};
export type Event = {
  event_id: string;
  source_id: string;
  source_event_id: string;
  entity_type: string;
  entity_id: string;
  entity_version: Int;
  status: string;
  attempt_count: Int;
  ingested_at: string;
  effective_at: string;
  occurred_at: string;
  available_at: string;
  outcome: string | null;
  reason_code: string | null;
  envelope_sha256: string;
  payload_sha256: string;
  committed_at: string | null;
};
export type Quarantine = {
  quarantine_id: string;
  event_id: string;
  reason_code: string;
  created_at: string;
  redacted_details: Json;
};
export type Report = {
  has_success: boolean;
  stale: boolean;
  observed_at: string;
  last_good: null | {
    run_id: string;
    source_id: string;
    interval_start: string;
    interval_end: string;
    published_at: string;
    manifest_sha256: string;
    storage: string;
    artifact_availability: string;
    summary: {
      order_count: Int;
      order_total_minor: Int;
      currency: string;
      product_count: Int;
      raw_revision_count: Int;
    };
    order_facts: {
      entity_id: string;
      entity_version: Int;
      total_minor: Int;
      currency: string;
      status: string;
    }[];
    product_history: {
      entity_id: string;
      entity_version: Int;
      valid_from: string;
      valid_to: string | null;
      unit_price_minor: Int;
      currency: string;
    }[];
    preview_limit: number;
    preview_truncated: boolean;
    manifest: Json;
  };
  latest_attempt: Run | null;
};
export type InventoryReport = {
  run_id: string;
  source_id: string;
  snapshot_id: string;
  reference_id: string;
  as_of: string;
  status: string;
  observed_sha256: string;
  reference_sha256: string;
  observed_total: Int;
  reference_total: Int;
  difference_total: Int;
  differences: {
    sku: string;
    observed_quantity: Int;
    reference_quantity: Int;
    difference: Int;
    observed_present: boolean;
    reference_present: boolean;
  }[];
  manifest_sha256: string;
  manifest: Json;
  storage: string;
  artifact_availability: string;
};
