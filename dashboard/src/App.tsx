import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type { FormEvent, ReactNode } from "react";
import { ApiError, json, money, number, request, time, title } from "./api";
import type {
  Config,
  Entity,
  Event,
  History,
  InventoryReport,
  Overview,
  Page,
  Quarantine,
  Report,
  Run,
  Session,
  Source,
} from "./types";

type Reader = <T>(path: string, options?: RequestInit) => Promise<T>;
const Workspace = createContext<{
  session: Session;
  read: Reader;
  refresh: number;
}>({} as never);
const nav = [
  ["overview", "Overview", "grid"],
  ["orders", "Current orders", "orders"],
  ["products", "Products & history", "box"],
  ["events", "Event inbox", "pulse"],
  ["quarantines", "Quarantines", "shield"],
  ["reports", "Runs & reports", "report"],
  ["inventory", "Inventory diff", "diff"],
] as const;
type View = (typeof nav)[number][0];
function Icon({ name, ...props }: { name: string; className?: string }) {
  const paths: Record<string, ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    orders: (
      <>
        <path d="M7 3h10v18l-5-3-5 3z" />
        <path d="M10 8h4m-4 4h4" />
      </>
    ),
    box: (
      <>
        <path d="m3 7 9-4 9 4v10l-9 4-9-4zM3 7l9 5 9-5M12 12v9" />
      </>
    ),
    pulse: <path d="M2 12h5l3-8 4 16 3-8h5" />,
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" />
        <path d="M12 8v5m0 3v.1" />
      </>
    ),
    report: (
      <>
        <path d="M5 3h10l4 4v14H5zM15 3v5h4M8 12h8m-8 4h6" />
      </>
    ),
    diff: (
      <>
        <path d="M4 7h16M4 17h16M8 3v8m8 2v8" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3" />
      </>
    ),
    arrow: <path d="m9 5 7 7-7 7" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    logout: (
      <>
        <path d="M9 3H4v18h5m6-14 5 5-5 5m-7-5h12" />
      </>
    ),
  };
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {paths[name] ?? paths.grid}
    </svg>
  );
}
function Badge({ value }: { value: string }) {
  return (
    <span
      className={`badge ${["completed", "pass", "published", "success", "succeeded", "enabled", "paid", "fulfilled", "applied_current"].includes(value) ? "good" : ["quarantined", "fail", "failed", "error", "cancelled", "disabled"].includes(value) ? "bad" : ["pending", "retry_wait", "stale", "running"].includes(value) ? "warn" : ""}`}
    >
      {title(value)}
    </span>
  );
}
function Empty({
  title: heading,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name="box" />
      <h3>{heading}</h3>
      <p>{children}</p>
    </div>
  );
}
function ErrorBox({ error, retry }: { error: unknown; retry?: () => void }) {
  const e =
    error instanceof Error
      ? error
      : new Error("The request could not be completed.");
  return (
    <div className="notice danger" role="alert">
      <strong>{e.message}</strong>
      {e instanceof ApiError && (
        <>
          <span>
            {e.status === 429
              ? "Request limit reached. Wait briefly before trying again."
              : e.status === 503
                ? "The service is temporarily unavailable."
                : e.code.replaceAll("_", " ")}
          </span>
          {e.requestId && (
            <small>
              Request ID: <code>{e.requestId}</code>
            </small>
          )}
        </>
      )}
      {retry && (
        <button className="button small" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
function useResource<T>(path: string) {
  const { read, refresh } = useContext(Workspace),
    [tick, setTick] = useState(0);
  const [state, setState] = useState<{
    key: string;
    data: T | null;
    error: unknown;
    busy: boolean;
  }>({ key: path, data: null, error: null, busy: true });
  useEffect(() => {
    const controller = new AbortController();
    setState((s) => ({
      key: path,
      data: s.key === path ? s.data : null,
      error: null,
      busy: true,
    }));
    read<T>(path, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted)
          setState({ key: path, data, error: null, busy: false });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState((s) => ({ ...s, error, busy: false }));
      });
    return () => controller.abort();
  }, [path, read, refresh, tick]);
  return {
    ...(state.key === path ? state : { data: null, error: null, busy: true }),
    retry: () => setTick((x) => x + 1),
  };
}
function Resource<T>({
  resource,
  children,
}: {
  resource: ReturnType<typeof useResource<T>>;
  children: (data: T) => ReactNode;
}) {
  return (
    <>
      {resource.error && (
        <ErrorBox error={resource.error} retry={resource.retry} />
      )}
      {resource.busy && (
        <div className="loading" role="status">
          <span className="spinner" />
          {resource.data
            ? "Refreshing this view…"
            : "Loading verified workspace data…"}
        </div>
      )}
      {resource.data ? (
        <>
          <div className="sr-only" role="status">
            {resource.busy ? "" : "View updated"}
          </div>
          {resource.error && (
            <div className="notice warning">
              Showing the last successfully loaded view. It may be out of date.
            </div>
          )}
          {children(resource.data)}
        </>
      ) : !resource.busy && !resource.error ? (
        <Empty title="No response available">Refresh to load this view.</Empty>
      ) : null}
    </>
  );
}
function Header({
  eyebrow,
  title: heading,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{heading}</h1>
      </div>
      {children}
    </div>
  );
}
function Panel({
  title: heading,
  subtitle,
  children,
  action,
  className = "",
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <div>
          <h2>{heading}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
function Stat({
  label,
  value,
  hint,
  tone = "",
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className={`stat ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}
function Definition({ values }: { values: [string, ReactNode][] }) {
  return (
    <dl className="definition">
      {values.map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
function Modal({
  title: heading,
  subtitle,
  children,
  close,
  busy = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  close: () => void;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null),
    id = useId();
  useEffect(() => {
    ref.current?.showModal();
    const el = ref.current;
    return () => el?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={id}
      aria-describedby={subtitle ? `${id}-sub` : undefined}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) close();
      }}
    >
      <div className="dialog-heading">
        <div>
          <p className="eyebrow">Inspect record</p>
          <h2 id={id}>{heading}</h2>
          {subtitle && <p id={`${id}-sub`}>{subtitle}</p>}
        </div>
        <button
          autoFocus
          aria-label="Close detail"
          className="icon-button"
          disabled={busy}
          onClick={close}
        >
          <Icon name="close" />
        </button>
      </div>
      <div className="dialog-content">{children}</div>
    </dialog>
  );
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null),
    [session, setSession] = useState<Session | null>(null),
    [identity, setIdentity] = useState(""),
    [connecting, setConnecting] = useState(false),
    [authError, setAuthError] = useState<unknown>(null),
    [configTick, setConfigTick] = useState(0);
  const [view, setView] = useState<View>(() =>
    nav.some(([key]) => key === location.hash.slice(1))
      ? (location.hash.slice(1) as View)
      : "overview",
  );
  const [refresh, setRefresh] = useState(0),
    [tokenInput, setTokenInput] = useState(""),
    [tenantInput, setTenantInput] = useState("");
  const epoch = useRef(0),
    tokenRef = useRef<string | null>(null);
  const endSession = useCallback((message?: string) => {
    epoch.current++;
    tokenRef.current = null;
    setSession(null);
    setConnecting(false);
    setTokenInput("");
    if (message) setAuthError(new Error(message));
  }, []);
  const connect = useCallback(
    async (id: string, token?: string, tenantId?: string) => {
      const mine = ++epoch.current;
      tokenRef.current = null;
      setSession(null);
      setConnecting(true);
      setAuthError(null);
      setIdentity(id);
      try {
        let next: Session;
        if (token)
          next = {
            access_token: token,
            principal: {
              tenant_id: tenantId ?? "",
              subject: "",
              role: "viewer",
            },
            tenant: { tenant_id: "", name: "", currency: "" },
          };
        else
          next = await request<Session>("/v1/dev/session", null, {
            method: "POST",
            body: JSON.stringify({ identity_id: id }),
          });
        const verified = await request<Pick<Session, "tenant" | "principal">>(
          "/v1/session",
          next,
        );
        if (
          !verified.principal?.tenant_id ||
          !verified.tenant?.name ||
          !["viewer", "operator"].includes(verified.principal.role)
        )
          throw new Error("The service returned an invalid session.");
        if (mine === epoch.current) {
          tokenRef.current = next.access_token;
          setSession({ ...next, ...verified });
          setTokenInput("");
        }
      } catch (error) {
        if (mine === epoch.current) {
          setAuthError(error);
          setIdentity("");
        }
      } finally {
        if (mine === epoch.current) setConnecting(false);
      }
    },
    [],
  );
  useEffect(() => {
    const controller = new AbortController();
    setAuthError(null);
    request<Config>("/v1/auth/config", null, { signal: controller.signal })
      .then((c) => {
        if (!controller.signal.aborted) {
          setConfig(c);
          if (c.fixture_login_enabled && c.identities.length)
            void connect(c.identities[0].id);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setAuthError(error);
      });
    return () => controller.abort();
  }, [configTick, connect]);
  useEffect(() => {
    const onHash = () => {
      const key = location.hash.slice(1);
      if (!key) setView("overview");
      else if (nav.some(([id]) => id === key)) setView(key as View);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const read: Reader = useCallback(
    async <T,>(path: string, options?: RequestInit) => {
      if (!session) throw new Error("Connect a session to continue.");
      const mine = epoch.current;
      try {
        const value = await request<T>(path, session, options);
        if (epoch.current !== mine || tokenRef.current !== session.access_token)
          throw new DOMException("Session changed", "AbortError");
        return value;
      } catch (error) {
        if (epoch.current !== mine || tokenRef.current !== session.access_token)
          throw new DOMException("Session changed", "AbortError");
        if (error instanceof ApiError && error.status === 401)
          endSession("Your session expired. Connect again to continue.");
        throw error;
      }
    },
    [session, endSession],
  );
  const changeView = (next: View) => {
    setView(next);
    location.hash = next;
  };
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to workspace
      </a>
      <div className="shell">
        <aside className="sidebar">
          <a
            className="brand"
            href="#overview"
            onClick={() => changeView("overview")}
            aria-label="OrderFlow overview"
          >
            <span className="brand-mark">
              O<span>F</span>
            </span>
            <span>
              OrderFlow<small>OPERATIONS WORKSPACE</small>
            </span>
          </a>
          <div className="nav-label">Workspace</div>
          <nav aria-label="Main navigation">
            {nav.map(([id, label, icon]) => (
              <a
                key={id}
                href={`#${id}`}
                aria-current={view === id ? "page" : undefined}
                onClick={() => changeView(id)}
              >
                <Icon name={icon} />
                <span>{label}</span>
                {view === id && <span className="nav-dot" />}
              </a>
            ))}
          </nav>
          <div className="sidebar-foot">
            <span className="status-dot" />
            <div>
              Local-first, evidence-led
              <small>Current records. Immutable history.</small>
            </div>
          </div>
        </aside>
        <div className="workspace">
          <header className="topbar">
            <div className="breadcrumb">
              <span>Operations</span>
              <Icon name="arrow" />
              <strong>{nav.find(([id]) => id === view)?.[1]}</strong>
            </div>
            <div className="session-tools">
              {session && (
                <>
                  <span className="tenant-name">{session.tenant.name}</span>
                  <Badge value={session.principal.role} />
                </>
              )}
              {config?.fixture_login_enabled && (
                <label className="identity-label">
                  <span className="sr-only">Local fixture identity</span>
                  <select
                    aria-label="Local fixture identity"
                    value={identity}
                    disabled={connecting}
                    onChange={(e) =>
                      e.target.value && void connect(e.target.value)
                    }
                  >
                    <option value="">Choose local identity</option>
                    {config.identities.map((i) => (
                      <option key={i.id} value={i.id}>
                        {i.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {session && (
                <>
                  <button
                    className="icon-button"
                    onClick={() => setRefresh((x) => x + 1)}
                    aria-label="Refresh workspace"
                  >
                    <Icon name="refresh" />
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => endSession()}
                    aria-label="Disconnect session"
                  >
                    <Icon name="logout" />
                  </button>
                </>
              )}
            </div>
          </header>
          <main id="main" tabIndex={-1}>
            {config?.fixture_login_enabled && (
              <div className="fixture-note">
                <span className="status-dot" /> Local fixture workspace ·
                Synthetic retailer data ·{" "}
                {session?.principal.role === "viewer"
                  ? "Read-only membership"
                  : "Operator actions are tenant-bound"}
              </div>
            )}
            {!session ? (
              <section className="connect-panel">
                <div className="connect-logo">
                  <Icon name="pulse" />
                </div>
                <p className="eyebrow">Secure workspace</p>
                <h1>
                  {connecting
                    ? "Connecting your workspace"
                    : "Connect to OrderFlow"}
                </h1>
                <p>
                  Inspect the current state, trace every revision, and review
                  quality outcomes within your verified tenant.
                </p>
                {connecting && (
                  <div className="loading" role="status">
                    <span className="spinner" />
                    Verifying session membership…
                  </div>
                )}
                {!!authError && (
                  <ErrorBox
                    error={authError}
                    retry={
                      !config ? () => setConfigTick((x) => x + 1) : undefined
                    }
                  />
                )}
                {config?.fixture_login_enabled ? (
                  <div className="connect-options">
                    <label htmlFor="login-identity">
                      Local fixture identity
                    </label>
                    <select
                      id="login-identity"
                      disabled={connecting}
                      value={identity}
                      onChange={(e) => setIdentity(e.target.value)}
                    >
                      <option value="">Choose identity</option>
                      {config.identities.map((i) => (
                        <option key={i.id} value={i.id}>
                          {i.label}
                        </option>
                      ))}
                    </select>
                    <button
                      className="button primary"
                      disabled={connecting || !identity}
                      onClick={() => void connect(identity)}
                    >
                      Connect local workspace
                    </button>
                    <small>
                      Local fixture sign-in is enabled by server configuration.
                      This is not an external identity-provider login.
                    </small>
                  </div>
                ) : (
                  config && (
                    <form
                      className="connect-options"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void connect("", tokenInput, tenantInput || undefined);
                      }}
                    >
                      <label htmlFor="bearer">Access token</label>
                      <input
                        id="bearer"
                        type="password"
                        autoComplete="off"
                        value={tokenInput}
                        required
                        onChange={(e) => setTokenInput(e.target.value)}
                      />
                      <label htmlFor="tenant">
                        Tenant ID{" "}
                        <span className="muted">
                          (optional membership selection)
                        </span>
                      </label>
                      <input
                        id="tenant"
                        value={tenantInput}
                        onChange={(e) => setTenantInput(e.target.value)}
                        autoComplete="off"
                      />
                      <button
                        className="button primary"
                        disabled={connecting || !tokenInput}
                      >
                        Verify and connect
                      </button>
                      <small>
                        Your token stays in memory and is cleared on disconnect.
                        Reloading requires a new session.
                      </small>
                    </form>
                  )
                )}
              </section>
            ) : (
              <Workspace.Provider
                key={session.access_token}
                value={{ session, read, refresh }}
              >
                {view === "overview" ? (
                  <OverviewPage navigate={changeView} />
                ) : view === "orders" ? (
                  <EntitiesPage key="orders" kind="orders" />
                ) : view === "products" ? (
                  <EntitiesPage key="products" kind="products" />
                ) : view === "events" ? (
                  <EventsPage />
                ) : view === "quarantines" ? (
                  <QuarantinesPage />
                ) : view === "reports" ? (
                  <ReportsPage />
                ) : (
                  <InventoryPage />
                )}
              </Workspace.Provider>
            )}
            <footer className="footer">
              <span>
                OrderFlow <span className="muted">/</span> Operations workbench
              </span>
              <span>UTC timestamps · Exact minor-unit amounts · v0.1</span>
            </footer>
          </main>
        </div>
      </div>
    </>
  );
}

function Sources({ items }: { items: Source[] }) {
  return items.length ? (
    <div className="source-grid">
      {items.map((s) => (
        <article className="source-card" key={s.source_id}>
          <div className="row">
            <span className="source-icon">
              <Icon name={s.connector_type === "csv" ? "report" : "pulse"} />
            </span>
            <Badge value={s.enabled ? "enabled" : "disabled"} />
          </div>
          <h3>{s.name}</h3>
          <p className="muted">
            {title(s.connector_type)} ·{" "}
            {s.authoritative_entity_types?.join(", ") || "No entity authority"}
          </p>
          <div className="source-counts">
            <div>
              <strong>{number(s.completed_count)}</strong>
              <span>Completed</span>
            </div>
            <div>
              <strong>{number(s.pending_count)}</strong>
              <span>Pending incl. retry</span>
            </div>
            <div>
              <strong>{number(s.quarantine_count)}</strong>
              <span>Quarantined</span>
            </div>
          </div>
          <dl className="mini-definition">
            <div>
              <dt>Last commit</dt>
              <dd>{time(s.last_commit_at)}</dd>
            </div>
            <div>
              <dt>Oldest pending</dt>
              <dd>
                {s.oldest_pending_at
                  ? time(s.oldest_pending_at)
                  : "No pending backlog"}
              </dd>
            </div>
            <div>
              <dt>Published interval end</dt>
              <dd>{time(s.last_successful_interval_end)}</dd>
            </div>
          </dl>
          <details>
            <summary>Source identity & counters</summary>
            <Definition
              values={[
                ["Source ID", <code>{s.source_id}</code>],
                ["Accepted", number(s.accepted_count)],
                ["Waiting to retry", number(s.retry_wait_count)],
              ]}
            />
          </details>
        </article>
      ))}
    </div>
  ) : (
    <Empty title="No sources registered">
      Sources appear here after they are configured for this tenant.
    </Empty>
  );
}
function OverviewPage({ navigate }: { navigate: (v: View) => void }) {
  const r = useResource<Overview>("/v1/overview");
  return (
    <>
      <Header eyebrow="Workspace pulse" title="Operations overview">
        <span className="subtle-pill">
          <span className="status-dot" />
          Tenant-scoped records
        </span>
      </Header>
      <p className="lead">
        One current record per entity. A traceable path from accepted event to
        published result.
      </p>
      <Resource resource={r}>
        {(d) => (
          <>
            <div className="stats">
              <Stat
                label="Current order amount"
                value={money(
                  d.order_totals.current_amount_minor,
                  d.order_totals.currency,
                )}
                hint={`${number(d.counts.orders)} current order snapshots`}
                tone="accent"
              />
              <Stat
                label="Current products"
                value={number(d.counts.products)}
                hint={`${number(d.counts.revisions)} immutable revisions`}
              />
              <Stat
                label="Pending work"
                value={number(
                  (
                    BigInt(d.counts.pending) + BigInt(d.counts.retry_wait)
                  ).toString(),
                )}
                hint={`${number(d.counts.retry_wait)} waiting to retry`}
              />
              <Stat
                label="Quarantined"
                value={number(d.counts.quarantined)}
                hint="Terminal records for review"
                tone={BigInt(d.counts.quarantined) ? "amber" : ""}
              />
            </div>
            <div className="overview-grid">
              <Panel
                title="Processing ledger"
                subtitle={`Observed ${time(d.observed_at)}`}
                action={
                  <button
                    className="text-button"
                    onClick={() => navigate("events")}
                  >
                    Open inbox <Icon name="arrow" />
                  </button>
                }
              >
                <div className="ledger-flow">
                  {[
                    ["Accepted", d.counts.accepted],
                    ["Completed", d.counts.completed],
                    [
                      "Pending work",
                      (
                        BigInt(d.counts.pending) + BigInt(d.counts.retry_wait)
                      ).toString(),
                    ],
                    ["Quarantined", d.counts.quarantined],
                  ].map(([label, value]) => (
                    <div key={String(label)}>
                      <span>{label}</span>
                      <strong>{number(value)}</strong>
                    </div>
                  ))}
                </div>
                <p className="caption">
                  Pending includes scheduled retries. Quarantined records remain
                  inspectable; they do not change current records.
                </p>
                <div className="freshness">
                  <span>
                    Last entity commit
                    <strong>{time(d.freshness.last_commit_at)}</strong>
                  </span>
                  <span>
                    Oldest pending event
                    <strong>
                      {d.freshness.oldest_pending_at
                        ? time(d.freshness.oldest_pending_at)
                        : "No pending backlog"}
                    </strong>
                  </span>
                </div>
              </Panel>
              <Panel
                title="Amount semantics"
                subtitle="Snapshot totals, not sales activity"
              >
                <div className="amount-line">
                  <span>Current order snapshots</span>
                  <strong>
                    {money(
                      d.order_totals.current_amount_minor,
                      d.order_totals.currency,
                    )}
                  </strong>
                </div>
                <div className="amount-line">
                  <span>Currently marked paid</span>
                  <strong>
                    {money(
                      d.order_totals.paid_amount_minor,
                      d.order_totals.currency,
                    )}
                  </strong>
                </div>
                <p className="caption">
                  Each order contributes its latest version once. Updates
                  replace a snapshot; these totals are not cumulative revenue.
                </p>
                <button className="button" onClick={() => navigate("orders")}>
                  Inspect current orders <Icon name="arrow" />
                </button>
              </Panel>
            </div>
            <Panel
              title="Source health"
              subtitle="Counts and freshness from the committed processing ledger"
            >
              <Sources items={d.sources} />
            </Panel>
            <Panel
              title="Recent pipeline attempts"
              subtitle="Quality checks remain available when an attempt fails"
              action={
                <button
                  className="text-button"
                  onClick={() => navigate("reports")}
                >
                  Reports & checks <Icon name="arrow" />
                </button>
              }
            >
              <RunRows runs={d.recent_runs} />
            </Panel>
          </>
        )}
      </Resource>
    </>
  );
}
function Pager({
  next,
  index,
  previous,
  advance,
  busy,
}: {
  next: string | null;
  index: number;
  previous: () => void;
  advance: () => void;
  busy: boolean;
}) {
  return (
    <div className="pager">
      <span>Page {index + 1} · Up to 25 records per page</span>
      <div>
        <button
          className="button small"
          disabled={!index || busy}
          onClick={previous}
        >
          Previous
        </button>
        <button
          className="button small"
          disabled={!next || busy}
          onClick={advance}
        >
          Next page <Icon name="arrow" />
        </button>
      </div>
    </div>
  );
}
function usePage<T>(base: string) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]),
    cursor = cursors[cursors.length - 1];
  const r = useResource<Page<T>>(
    base +
      (base.includes("?") ? "&" : "?") +
      "limit=25" +
      (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""),
  );
  return {
    r,
    pager: {
      next: r.data?.next_cursor ?? null,
      index: cursors.length - 1,
      previous: () => setCursors((v) => v.slice(0, -1)),
      advance: () => {
        if (r.data?.next_cursor) setCursors((v) => [...v, r.data!.next_cursor]);
      },
      busy: r.busy,
    },
  };
}
function EntitiesPage({ kind }: { kind: "orders" | "products" }) {
  const { r, pager } = usePage<Entity>(`/v1/current/${kind}`),
    [selected, setSelected] = useState<Entity | null>(null);
  return (
    <>
      <Header
        eyebrow="Current state"
        title={kind === "orders" ? "Current orders" : "Products & history"}
      >
        <span className="subtle-pill">Latest version per entity</span>
      </Header>
      <p className="lead">
        {kind === "orders"
          ? "Inspect the committed order snapshot. Event replay and older revisions do not add to this total."
          : "Follow the latest product snapshot back through every retained revision and effective interval."}
      </p>
      <Panel
        title={kind === "orders" ? "Order snapshots" : "Product catalogue"}
        subtitle="Keyset pagination · Stable entity identifiers"
      >
        <Resource resource={r}>
          {(d) => (
            <>
              {d.items.length ? (
                <div
                  className="table-wrap"
                  tabIndex={0}
                  role="region"
                  aria-label="Scrollable data table"
                >
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">
                          {kind === "orders" ? "Order" : "Product"}
                        </th>
                        <th scope="col">
                          {kind === "orders" ? "Customer" : "SKU"}
                        </th>
                        <th scope="col">
                          {kind === "orders" ? "Amount" : "Unit price"}
                        </th>
                        <th scope="col">
                          {kind === "orders" ? "Status" : "Version"}
                        </th>
                        <th scope="col">Effective at</th>
                        <th scope="col">
                          <span className="sr-only">Inspect</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.items.map((e) => (
                        <tr key={e.entity_id}>
                          <th scope="row">
                            <button
                              className="record-link"
                              onClick={() => setSelected(e)}
                            >
                              {e.entity_id}
                            </button>
                            {kind === "products" && (
                              <small>{e.payload.name}</small>
                            )}
                          </th>
                          <td>
                            {kind === "orders"
                              ? e.payload.customer_id
                              : e.payload.sku}
                          </td>
                          <td className="numeric">
                            {money(
                              kind === "orders"
                                ? e.payload.total_minor
                                : e.payload.unit_price_minor,
                              e.payload.currency,
                            )}
                          </td>
                          <td>
                            {kind === "orders" ? (
                              <Badge value={e.payload.status ?? "unknown"} />
                            ) : (
                              <code>v{e.entity_version}</code>
                            )}
                          </td>
                          <td className="date-cell">{time(e.effective_at)}</td>
                          <td>
                            <button
                              className="icon-button"
                              onClick={() => setSelected(e)}
                              aria-label={`Inspect ${kind === "products" ? "history for" : "order"} ${e.entity_id}`}
                            >
                              <Icon name="arrow" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title={`No current ${kind}`}>
                  Committed {kind === "products" ? "product" : "order"} events
                  will appear here for this tenant.
                </Empty>
              )}
              <Pager {...pager} />
            </>
          )}
        </Resource>
      </Panel>
      {selected &&
        (kind === "products" ? (
          <HistoryDetail entity={selected} close={() => setSelected(null)} />
        ) : (
          <Modal
            title={`Order ${selected.entity_id}`}
            subtitle="Current committed snapshot"
            close={() => setSelected(null)}
          >
            <Definition
              values={[
                ["Order", selected.entity_id],
                ["Version", <code>{selected.entity_version}</code>],
                ["Customer", selected.payload.customer_id],
                [
                  "Amount",
                  money(
                    selected.payload.total_minor,
                    selected.payload.currency,
                  ),
                ],
                [
                  "Status",
                  <Badge value={selected.payload.status ?? "unknown"} />,
                ],
                ["Effective at", time(selected.effective_at)],
                ["Source ID", <code>{selected.source_id}</code>],
                ["Committed event", <code>{selected.event_id}</code>],
              ]}
            />
            <p className="caption">
              The event inbox provides processing outcomes and integrity hashes.
              This snapshot does not expose a customer profile.
            </p>
          </Modal>
        ))}
    </>
  );
}
function HistoryChart({
  rows,
  currency,
}: {
  rows: History["intervals"];
  currency: string;
}) {
  if (!rows.length) return null;
  const sorted = [...rows].sort(
      (a, b) => Date.parse(a.valid_from!) - Date.parse(b.valid_from!),
    ),
    values = sorted.map((r) => Number(r.payload.unit_price_minor ?? 0)),
    max = Math.max(1, ...values),
    minTime = Date.parse(sorted[0].valid_from!),
    lastTime = Date.parse(sorted[sorted.length - 1].valid_from!),
    span = lastTime - minTime || 1;
  const x = (r: History["intervals"][number]) =>
      55 + ((Date.parse(r.valid_from!) - minTime) / span) * 635,
    y = (v: number) => 155 - (v / max) * 120;
  const path =
    sorted
      .map(
        (r, i) =>
          `${i ? `H${x(r)}V${y(values[i])}` : `M${x(r)},${y(values[i])}`}`,
      )
      .join(" ") + `H730`;
  return (
    <figure className="history-chart">
      <figcaption>
        <strong>Effective price history</strong>
        <span>Step changes at curated interval boundaries · {currency}</span>
      </figcaption>
      <svg
        viewBox="0 0 760 205"
        role="img"
        aria-label={`Effective price across ${rows.length} curated intervals. Exact values are listed in the following table.`}
      >
        <path
          d="M55 35H730M55 95H730M55 155H730"
          stroke="#dce6e8"
          strokeDasharray="4 5"
        />
        <text x="50" y="27">
          {money(max, currency)}
        </text>
        <text x="50" y="180">
          {time(sorted[0].valid_from).split(" UTC")[0]}
        </text>
        <path d={path} stroke="#087e75" fill="none" strokeWidth="3" />
        {sorted.map((r, i) => (
          <circle
            key={`${r.entity_version}-${i}`}
            cx={x(r)}
            cy={y(values[i])}
            r="4"
            fill="#087e75"
          >
            <title>
              Version {r.entity_version}:{" "}
              {money(r.payload.unit_price_minor, currency)} from{" "}
              {time(r.valid_from)}
            </title>
          </circle>
        ))}
      </svg>
    </figure>
  );
}
function HistoryDetail({
  entity,
  close,
}: {
  entity: Entity;
  close: () => void;
}) {
  const r = useResource<History>(
      `/v1/product-history?entity_id=${encodeURIComponent(entity.entity_id)}`,
    ),
    [tab, setTab] = useState<"intervals" | "raw">("intervals");
  return (
    <Modal
      title={entity.payload.name || entity.entity_id}
      subtitle={`Product ${entity.entity_id} · ${entity.payload.sku}`}
      close={close}
    >
      <Resource resource={r}>
        {(d) => (
          <>
            <HistoryChart
              rows={d.intervals}
              currency={entity.payload.currency}
            />
            <div className="segmented" role="group" aria-label="History view">
              <button
                aria-pressed={tab === "intervals"}
                onClick={() => setTab("intervals")}
              >
                Effective intervals ({d.intervals.length})
              </button>
              <button
                aria-pressed={tab === "raw"}
                onClick={() => setTab("raw")}
              >
                Raw revisions ({d.raw_revisions.length})
              </button>
            </div>
            <p className="caption">
              {tab === "intervals"
                ? "Intervals are half-open [from, to). A higher version wins when effective timestamps are equal. An open end means the final known interval."
                : "Every immutable revision is retained, including versions that arrived out of order or share an effective timestamp."}
            </p>
            {(tab === "intervals" ? d.intervals : d.raw_revisions).length ? (
              <div
                className="table-wrap"
                tabIndex={0}
                role="region"
                aria-label="Scrollable data table"
              >
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">Name / SKU</th>
                      <th scope="col">Price</th>
                      <th scope="col">
                        {tab === "intervals" ? "Valid from" : "Effective at"}
                      </th>
                      <th scope="col">
                        {tab === "intervals"
                          ? "Valid to (exclusive)"
                          : "Event ID"}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(tab === "intervals" ? d.intervals : d.raw_revisions).map(
                      (v, i) => (
                        <tr key={`${v.entity_version}-${i}`}>
                          <th scope="row">
                            <code>v{v.entity_version}</code>
                          </th>
                          <td>
                            {v.payload.name}
                            <small>{v.payload.sku}</small>
                          </td>
                          <td className="numeric">
                            {money(
                              v.payload.unit_price_minor,
                              v.payload.currency,
                            )}
                          </td>
                          <td>
                            {time(
                              tab === "intervals"
                                ? v.valid_from
                                : v.effective_at,
                            )}
                          </td>
                          <td>
                            {tab === "intervals" ? (
                              v.valid_to ? (
                                time(v.valid_to)
                              ) : (
                                <span className="open-end">Open end</span>
                              )
                            ) : (
                              <code>{v.event_id}</code>
                            )}
                          </td>
                        </tr>
                      ),
                    )}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="No history available">
                No committed revisions are available for this product.
              </Empty>
            )}
          </>
        )}
      </Resource>
    </Modal>
  );
}

function EventsPage() {
  const [status, setStatus] = useState(""),
    [source, setSource] = useState(""),
    sources = useResource<{ items: Source[] }>("/v1/sources");
  return (
    <>
      <Header eyebrow="Processing ledger" title="Event inbox" />
      <p className="lead">
        Trace an accepted event through retries, immutable outcomes, and
        current-state application.
      </p>
      <div className="filters">
        <label>
          Status
          <select
            aria-label="Status"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">All statuses</option>
            {["pending", "retry_wait", "completed", "quarantined"].map((s) => (
              <option key={s} value={s}>
                {title(s)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Source
          <select
            aria-label="Source"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">All sources</option>
            {sources.data?.items.map((s) => (
              <option key={s.source_id} value={s.source_id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        {(status || source) && (
          <button
            className="text-button"
            onClick={() => {
              setStatus("");
              setSource("");
            }}
          >
            Clear filters
          </button>
        )}
      </div>
      {sources.error && (
        <ErrorBox error={sources.error} retry={sources.retry} />
      )}
      <EventList
        key={`${status}|${source}`}
        status={status}
        source={source}
        refreshSources={sources.retry}
      />
      <Panel
        title="Source health"
        subtitle="Pending includes retry-wait events; terminal quarantine is counted separately"
      >
        <Resource resource={sources}>
          {(d) => <Sources items={d.items} />}
        </Resource>
      </Panel>
    </>
  );
}
function EventList({
  status,
  source,
  refreshSources,
}: {
  status: string;
  source: string;
  refreshSources: () => void;
}) {
  const path =
      "/v1/events?" +
      new URLSearchParams({
        ...(status ? { status } : {}),
        ...(source ? { source_id: source } : {}),
      }),
    { r, pager } = usePage<Event>(path),
    [event, setEvent] = useState<string | null>(null);
  return (
    <>
      <Panel title="Accepted events" subtitle="Opaque, tenant-bound pagination">
        <Resource resource={r}>
          {(d) => (
            <>
              {d.items.length ? (
                <div
                  className="table-wrap"
                  tabIndex={0}
                  role="region"
                  aria-label="Scrollable data table"
                >
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Entity / source event</th>
                        <th scope="col">Version</th>
                        <th scope="col">Status</th>
                        <th scope="col">Attempts</th>
                        <th scope="col">Ingested at</th>
                        <th scope="col">Outcome</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.items.map((e) => (
                        <tr key={e.event_id}>
                          <th scope="row">
                            <button
                              className="record-link"
                              onClick={() => setEvent(e.event_id)}
                            >
                              {e.entity_id}
                            </button>
                            <small>{e.source_event_id}</small>
                          </th>
                          <td>
                            <code>v{e.entity_version}</code>
                            <small>{title(e.entity_type)}</small>
                          </td>
                          <td>
                            <Badge value={e.status} />
                          </td>
                          <td>{number(e.attempt_count)}</td>
                          <td>{time(e.ingested_at)}</td>
                          <td>
                            {e.outcome ? title(e.outcome) : "Awaiting outcome"}
                            {e.reason_code && (
                              <small>{title(e.reason_code)}</small>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="No matching events">
                  {status || source
                    ? "Try a different status or source filter."
                    : "Accepted source events will appear here."}
                </Empty>
              )}
              <Pager {...pager} />
            </>
          )}
        </Resource>
      </Panel>
      {event && (
        <EventDetail
          key={event}
          id={event}
          close={() => {
            setEvent(null);
            r.retry();
            refreshSources();
          }}
        />
      )}
    </>
  );
}
function EventDetail({ id, close }: { id: string; close: () => void }) {
  const r = useResource<Event>(`/v1/events/${encodeURIComponent(id)}`),
    { session, read } = useContext(Workspace),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [ack, setAck] = useState(""),
    [verifying, setVerifying] = useState(false);
  const retrySnapshot = useRef<Event | null>(null);
  useEffect(() => {
    if (
      verifying &&
      !busy &&
      !r.busy &&
      !r.error &&
      r.data &&
      r.data !== retrySnapshot.current
    )
      setVerifying(false);
  }, [verifying, busy, r.busy, r.error, r.data]);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const retry = async (e: FormEvent) => {
    e.preventDefault();
    if (
      busy ||
      verifying ||
      r.busy ||
      r.error ||
      r.data?.status !== "retry_wait" ||
      session.principal.role !== "operator"
    )
      return;
    retrySnapshot.current = r.data;
    setVerifying(true);
    setBusy(true);
    setError(null);
    setAck("");
    try {
      await read(`/v1/events/${encodeURIComponent(id)}/retry`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      if (active.current) {
        setAck(
          "Retry requested. Refreshing the event to verify its current state.",
        );
        setReason("");
        r.retry();
      }
    } catch (failure) {
      if (active.current) {
        setError(failure);
        r.retry();
      }
    } finally {
      if (active.current) setBusy(false);
    }
  };
  return (
    <Modal title="Event detail" subtitle={id} close={close} busy={busy}>
      <Resource resource={r}>
        {(d) => (
          <>
            <div className="detail-status">
              <Badge value={d.status} />
              <span>
                {d.outcome ? title(d.outcome) : "No terminal outcome yet"}
              </span>
            </div>
            <Definition
              values={[
                ["Entity", `${d.entity_type}: ${d.entity_id}`],
                ["Version", <code>{d.entity_version}</code>],
                ["Source event", d.source_event_id],
                ["Source ID", <code>{d.source_id}</code>],
                ["Attempts", number(d.attempt_count)],
                ["Reason code", d.reason_code ? title(d.reason_code) : "None"],
                ["Ingested at", time(d.ingested_at)],
                ["Occurred at", time(d.occurred_at)],
                ["Effective at", time(d.effective_at)],
                ["Available at", time(d.available_at)],
                ["Committed at", time(d.committed_at)],
              ]}
            />
            <details>
              <summary>Integrity hashes</summary>
              <Definition
                values={[
                  ["Envelope SHA-256", <code>{d.envelope_sha256}</code>],
                  ["Payload SHA-256", <code>{d.payload_sha256}</code>],
                ]}
              />
            </details>
            {d.status === "retry_wait" && !verifying && !r.busy && !r.error ? (
              <section className="action-panel">
                <h3>Request a bounded retry</h3>
                <p>
                  The worker will try this event again. A terminal receipt
                  cannot be replayed by this action.
                </p>
                {session.principal.role === "operator" ? (
                  <form onSubmit={(e) => void retry(e)}>
                    <label htmlFor="retry-reason">
                      Operational reason{" "}
                      <small>
                        8–500 characters. Avoid personal or customer
                        information.
                      </small>
                    </label>
                    <textarea
                      id="retry-reason"
                      minLength={8}
                      maxLength={500}
                      required
                      disabled={busy}
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                    />
                    <button
                      className="button primary"
                      disabled={busy || reason.length < 8}
                    >
                      {busy ? "Requesting retry…" : "Request retry"}
                    </button>
                  </form>
                ) : (
                  <div className="notice">
                    Your viewer membership can inspect this event. An operator
                    must request a retry.
                  </div>
                )}
              </section>
            ) : d.status === "quarantined" ? (
              <div className="notice warning">
                This is a terminal quarantine. Correct the source data and
                submit a new source-event identity and entity version; this
                record cannot be reopened.
              </div>
            ) : (
              <p className="caption">
                {d.status === "completed"
                  ? "This committed terminal outcome is immutable. Replaying the same source event returns its existing result."
                  : "The worker owns pending processing. Refresh this view to inspect the next committed state."}
              </p>
            )}
            {verifying && (
              <div className="loading" role="status">
                <span className="spinner" />
                Verifying the event state before another action…
              </div>
            )}
            {error && (
              <>
                <ErrorBox error={error} />
                <p className="caption">
                  A lost response may follow a committed retry. The event above
                  is refreshed before another attempt is offered.
                </p>
              </>
            )}
            {ack && (
              <div className="notice success" role="status">
                {ack}
              </div>
            )}
          </>
        )}
      </Resource>
    </Modal>
  );
}
function QuarantinesPage() {
  const { r, pager } = usePage<Quarantine>("/v1/quarantines"),
    [selected, setSelected] = useState<Quarantine | null>(null),
    [event, setEvent] = useState<string | null>(null);
  return (
    <>
      <Header eyebrow="Exception review" title="Quarantines" />
      <p className="lead">
        Terminal, inspectable outcomes. Review the reason, correct the source,
        and preserve the original record.
      </p>
      <div className="notice warning">
        <strong>Quarantine is not a retry queue.</strong>
        <span>
          Only retry-wait events can be retried. Terminal corrections require a
          new source-event identity and entity version.
        </span>
      </div>
      <Panel
        title="Terminal exceptions"
        subtitle="Tenant-bound records with redacted diagnostic details"
      >
        <Resource resource={r}>
          {(d) => (
            <>
              {d.items.length ? (
                <div
                  className="table-wrap"
                  tabIndex={0}
                  role="region"
                  aria-label="Scrollable data table"
                >
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Quarantine</th>
                        <th scope="col">Reason</th>
                        <th scope="col">Created at</th>
                        <th scope="col">Review</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.items.map((q) => (
                        <tr key={q.quarantine_id}>
                          <th scope="row">
                            <button
                              className="record-link"
                              onClick={() => setSelected(q)}
                            >
                              {q.quarantine_id}
                            </button>
                            <small>Event {q.event_id}</small>
                          </th>
                          <td>
                            <Badge value={q.reason_code} />
                          </td>
                          <td>{time(q.created_at)}</td>
                          <td>
                            <button
                              className="button small"
                              onClick={() => setEvent(q.event_id)}
                            >
                              Inspect event
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="No quarantines">
                  No terminal exceptions are recorded for this tenant.
                </Empty>
              )}
              <Pager {...pager} />
            </>
          )}
        </Resource>
      </Panel>
      {selected && (
        <Modal
          title="Quarantine record"
          subtitle={selected.quarantine_id}
          close={() => setSelected(null)}
        >
          <Definition
            values={[
              ["Event ID", <code>{selected.event_id}</code>],
              ["Reason", title(selected.reason_code)],
              ["Created at", time(selected.created_at)],
            ]}
          />
          <h3>Redacted diagnostic details</h3>
          <pre>{json(selected.redacted_details)}</pre>
          <p className="caption">
            The original record stays immutable. Submit corrected source data
            under a new event identity and version.
          </p>
        </Modal>
      )}
      {event && (
        <EventDetail key={event} id={event} close={() => setEvent(null)} />
      )}
    </>
  );
}

function RunRows({
  runs,
  inspect,
}: {
  runs: Run[];
  inspect?: (id: string) => void;
}) {
  return runs.length ? (
    <div
      className="table-wrap"
      tabIndex={0}
      role="region"
      aria-label="Scrollable data table"
    >
      <table>
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">Status</th>
            <th scope="col">Interval</th>
            <th scope="col">Finished</th>
            <th scope="col">Failure code</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id}>
              <th scope="row">
                {inspect ? (
                  <button
                    className="record-link"
                    onClick={() => inspect(r.run_id)}
                  >
                    {r.run_id}
                  </button>
                ) : (
                  <code>{r.run_id}</code>
                )}
              </th>
              <td>
                <Badge value={r.status} />
              </td>
              <td>
                {time(r.interval_start)}
                <small>to {time(r.interval_end)}</small>
              </td>
              <td>{r.finished_at ? time(r.finished_at) : "Not finished"}</td>
              <td>
                {r.failure_code ? title(r.failure_code) : "None recorded"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty title="No publication attempts">
      A completed pipeline run will create an inspectable result here.
    </Empty>
  );
}
function Checks({ checks }: { checks: NonNullable<Run["checks"]> }) {
  return checks.length ? (
    <div
      className="table-wrap"
      tabIndex={0}
      role="region"
      aria-label="Scrollable data table"
    >
      <table>
        <thead>
          <tr>
            <th scope="col">Quality check</th>
            <th scope="col">Status</th>
            <th scope="col">Observed</th>
            <th scope="col">Expected</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((c, i) => (
            <tr key={`${c.name}-${i}`}>
              <th scope="row">{title(c.name)}</th>
              <td>
                <Badge value={c.status} />
              </td>
              <td>
                <pre className="cell-json">{json(c.observed)}</pre>
              </td>
              <td>
                <pre className="cell-json">{json(c.expected)}</pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <p className="caption">No quality checks were recorded for this attempt.</p>
  );
}
function RunDetail({ id, close }: { id: string; close: () => void }) {
  const r = useResource<Run>(`/v1/runs/${encodeURIComponent(id)}`);
  return (
    <Modal title="Run & quality checks" subtitle={id} close={close}>
      <Resource resource={r}>
        {(d) => (
          <>
            <div className="detail-status">
              <Badge value={d.status} />
              {d.failure_code && <span>{title(d.failure_code)}</span>}
            </div>
            <Definition
              values={[
                ["Source ID", <code>{d.source_id}</code>],
                ["Interval start", time(d.interval_start)],
                ["Interval end", time(d.interval_end)],
                ["Started at", time(d.started_at)],
                ["Finished at", time(d.finished_at)],
              ]}
            />
            <h3>Recorded quality checks</h3>
            <Checks checks={d.checks ?? []} />
          </>
        )}
      </Resource>
    </Modal>
  );
}
function ReportsPage() {
  const r = useResource<Report>("/v1/reports/latest"),
    overview = useResource<Overview>("/v1/overview"),
    [run, setRun] = useState<string | null>(null),
    [preview, setPreview] = useState<"orders" | "products">("orders");
  return (
    <>
      <Header eyebrow="Publication & quality" title="Runs & reports" />
      <p className="lead">
        A failed attempt never replaces the last successful publication. Inspect
        both the result and the checks behind it.
      </p>
      <Resource resource={r}>
        {(d) => (
          <>
            {d.has_success && d.last_good && d.stale && (
              <div className="notice warning">
                <strong>Last successful publication is stale.</strong>
                <span>
                  The last-good result remains available. Review its interval
                  and the latest attempt before using it.
                </span>
              </div>
            )}
            {!d.has_success || !d.last_good ? (
              <Panel title="Published report">
                <Empty title="No successful publication yet">
                  This workspace has no last-good report. A failed or pending
                  attempt is not a healthy empty report.
                </Empty>
              </Panel>
            ) : (
              <>
                <div className="stats">
                  <Stat
                    label="Published order snapshots"
                    value={number(d.last_good.summary.order_count)}
                  />
                  <Stat
                    label="Published order amount"
                    value={money(
                      d.last_good.summary.order_total_minor,
                      d.last_good.summary.currency,
                    )}
                    tone="accent"
                  />
                  <Stat
                    label="Published products"
                    value={number(d.last_good.summary.product_count)}
                  />
                  <Stat
                    label="Raw revisions"
                    value={number(d.last_good.summary.raw_revision_count)}
                  />
                </div>
                <Panel
                  title="Last successful publication"
                  subtitle={`Published ${time(d.last_good.published_at)}`}
                  action={<Badge value={d.stale ? "stale" : "published"} />}
                >
                  <Definition
                    values={[
                      [
                        "Run",
                        <button
                          className="record-link"
                          onClick={() => setRun(d.last_good!.run_id)}
                        >
                          {d.last_good.run_id}
                        </button>,
                      ],
                      ["Interval start", time(d.last_good.interval_start)],
                      [
                        "Interval end (exclusive)",
                        time(d.last_good.interval_end),
                      ],
                      [
                        "Artifact storage",
                        "Local artifacts · not a cloud export",
                      ],
                      [
                        "Manifest SHA-256",
                        <code>{d.last_good.manifest_sha256}</code>,
                      ],
                    ]}
                  />
                  <div
                    className="segmented"
                    role="group"
                    aria-label="Published data preview"
                  >
                    <button
                      aria-pressed={preview === "orders"}
                      onClick={() => setPreview("orders")}
                    >
                      Order facts
                    </button>
                    <button
                      aria-pressed={preview === "products"}
                      onClick={() => setPreview("products")}
                    >
                      Product intervals
                    </button>
                  </div>
                  <p className="caption">
                    Preview capped at {d.last_good.preview_limit} rows.
                    {d.last_good.preview_truncated
                      ? " Additional rows are present in local artifacts."
                      : ""}{" "}
                    Published facts describe this interval, not the live
                    overview snapshot.
                  </p>
                  {preview === "orders" ? (
                    d.last_good.order_facts.length ? (
                      <div
                        className="table-wrap"
                        tabIndex={0}
                        role="region"
                        aria-label="Scrollable data table"
                      >
                        <table>
                          <thead>
                            <tr>
                              <th scope="col">Order</th>
                              <th scope="col">Version</th>
                              <th scope="col">Amount</th>
                              <th scope="col">Status</th>
                            </tr>
                          </thead>
                          <tbody>
                            {d.last_good.order_facts.map((o, i) => (
                              <tr key={`${o.entity_id}-${i}`}>
                                <th scope="row">{o.entity_id}</th>
                                <td>
                                  <code>{o.entity_version}</code>
                                </td>
                                <td>{money(o.total_minor, o.currency)}</td>
                                <td>
                                  <Badge value={o.status} />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <Empty title="No order facts in this interval" />
                    )
                  ) : d.last_good.product_history.length ? (
                    <div
                      className="table-wrap"
                      tabIndex={0}
                      role="region"
                      aria-label="Scrollable data table"
                    >
                      <table>
                        <thead>
                          <tr>
                            <th scope="col">Product</th>
                            <th scope="col">Version</th>
                            <th scope="col">Price</th>
                            <th scope="col">From</th>
                            <th scope="col">To (exclusive)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {d.last_good.product_history.map((p, i) => (
                            <tr key={`${p.entity_id}-${i}`}>
                              <th scope="row">{p.entity_id}</th>
                              <td>
                                <code>{p.entity_version}</code>
                              </td>
                              <td>{money(p.unit_price_minor, p.currency)}</td>
                              <td>{time(p.valid_from)}</td>
                              <td>
                                {p.valid_to ? time(p.valid_to) : "Open end"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty title="No product intervals in this publication" />
                  )}
                  <details>
                    <summary>Publication manifest</summary>
                    <pre>{json(d.last_good.manifest)}</pre>
                  </details>
                </Panel>
              </>
            )}
            {d.latest_attempt && (
              <Panel
                title="Latest attempt"
                subtitle={`Started ${time(d.latest_attempt.started_at)}`}
                action={
                  <button
                    className="button small"
                    onClick={() => setRun(d.latest_attempt!.run_id)}
                  >
                    Inspect run
                  </button>
                }
              >
                <RunRows runs={[d.latest_attempt]} inspect={setRun} />
                <Checks checks={d.latest_attempt.checks ?? []} />
              </Panel>
            )}
          </>
        )}
      </Resource>
      <Panel
        title="Recent attempts"
        subtitle="Latest ten recorded runs for this tenant"
      >
        <Resource resource={overview}>
          {(d) => <RunRows runs={d.recent_runs} inspect={setRun} />}
        </Resource>
      </Panel>
      {run && <RunDetail key={run} id={run} close={() => setRun(null)} />}
    </>
  );
}

function InventoryPage() {
  const sources = useResource<{ items: Source[] }>("/v1/sources"),
    [source, setSource] = useState("");
  return (
    <>
      <Header eyebrow="Independent reconciliation" title="Inventory diff" />
      <p className="lead">
        Compare a received CSV snapshot with its reference. A failed comparison
        stays inspectable and never mutates orders or products.
      </p>
      <div className="filters">
        <label>
          Source
          <select
            aria-label="Source"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">All inventory sources</option>
            {sources.data?.items
              .filter((s) => s.connector_type === "csv")
              .map((s) => (
                <option key={s.source_id} value={s.source_id}>
                  {s.name}
                </option>
              ))}
          </select>
        </label>
      </div>
      {sources.error && (
        <ErrorBox error={sources.error} retry={sources.retry} />
      )}
      <InventoryContent key={source} source={source} />
    </>
  );
}
function InventoryContent({ source }: { source: string }) {
  const query = source ? `?source_id=${encodeURIComponent(source)}` : "",
    r = useResource<{ has_snapshot: boolean; latest: InventoryReport | null }>(
      `/v1/inventory/latest${query}`,
    ),
    history = useResource<{ items: InventoryReport[]; truncated: boolean }>(
      `/v1/inventory/history${query}${query ? "&" : "?"}limit=20`,
    ),
    [selected, setSelected] = useState<InventoryReport | null>(null);
  return (
    <>
      <Resource resource={r}>
        {(d) =>
          d.has_snapshot && d.latest ? (
            <InventoryResult report={d.latest} />
          ) : (
            <Panel title="Latest comparison">
              <Empty title="No inventory snapshot yet">
                An inventory connector run creates the first comparison. No
                quantities are inferred from missing data.
              </Empty>
            </Panel>
          )
        }
      </Resource>
      <Panel
        title="Comparison history"
        subtitle="Latest twenty snapshots · Failed comparisons remain available"
      >
        <Resource resource={history}>
          {(d) => (
            <>
              {d.items.length ? (
                <div
                  className="table-wrap"
                  tabIndex={0}
                  role="region"
                  aria-label="Scrollable data table"
                >
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Snapshot</th>
                        <th scope="col">Result</th>
                        <th scope="col">As of</th>
                        <th scope="col">Observed / reference</th>
                        <th scope="col">Difference</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.items.map((i) => (
                        <tr key={i.run_id}>
                          <th scope="row">
                            <button
                              className="record-link"
                              onClick={() => setSelected(i)}
                            >
                              {i.snapshot_id}
                            </button>
                            <small>Reference {i.reference_id}</small>
                          </th>
                          <td>
                            <Badge value={i.status} />
                          </td>
                          <td>{time(i.as_of)}</td>
                          <td>
                            {number(i.observed_total)} /{" "}
                            {number(i.reference_total)}
                          </td>
                          <td className="numeric">
                            {number(i.difference_total)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="No comparison history" />
              )}
              {d.truncated && (
                <p className="caption">
                  Showing the latest twenty comparisons. Earlier runs remain in
                  local audit records.
                </p>
              )}
            </>
          )}
        </Resource>
      </Panel>
      {selected && (
        <Modal
          title="Inventory comparison"
          subtitle={selected.snapshot_id}
          close={() => setSelected(null)}
        >
          <InventoryResult report={selected} historical />
        </Modal>
      )}
    </>
  );
}
function InventoryResult({
  report: d,
  historical = false,
}: {
  report: InventoryReport;
  historical?: boolean;
}) {
  const max = [BigInt(d.observed_total), BigInt(d.reference_total), 1n].reduce(
      (a, b) => (a > b ? a : b),
    ),
    width = (v: number | string) => Number((BigInt(v) * 10000n) / max) / 100;
  return (
    <Panel
      title={
        historical ? "Recorded comparison result" : "Latest comparison result"
      }
      subtitle={`Snapshot ${d.snapshot_id} · As of ${time(d.as_of)}`}
      action={<Badge value={d.status} />}
    >
      <div className="reconcile-grid">
        <div
          className="reconcile-bars"
          role="img"
          aria-label={`Observed quantity ${number(d.observed_total)}, reference quantity ${number(d.reference_total)}, net difference ${number(d.difference_total)}.`}
        >
          <div>
            <span>Observed quantity</span>
            <strong>{number(d.observed_total)}</strong>
            <i style={{ width: `${width(d.observed_total)}%` }} />
          </div>
          <div>
            <span>Reference quantity</span>
            <strong>{number(d.reference_total)}</strong>
            <i
              className="reference"
              style={{ width: `${width(d.reference_total)}%` }}
            />
          </div>
        </div>
        <div className="delta">
          <span>Net quantity difference</span>
          <strong>
            {BigInt(d.difference_total) > 0n ? "+" : ""}
            {number(d.difference_total)}
          </strong>
          <small>
            Net totals can cancel out. Review per-SKU differences below.
          </small>
        </div>
      </div>
      <p className="caption">
        Observed snapshot and reference are independently identified. Quantities
        are units, not monetary values. Local artifacts only.
      </p>
      {d.differences.length ? (
        <div
          className="table-wrap"
          tabIndex={0}
          role="region"
          aria-label="Scrollable data table"
        >
          <table>
            <thead>
              <tr>
                <th scope="col">SKU</th>
                <th scope="col">Observed</th>
                <th scope="col">Reference</th>
                <th scope="col">Difference</th>
                <th scope="col">Presence</th>
              </tr>
            </thead>
            <tbody>
              {d.differences.map((row, i) => (
                <tr key={`${row.sku}-${i}`}>
                  <th scope="row">{row.sku}</th>
                  <td>
                    {row.observed_present
                      ? number(row.observed_quantity)
                      : "Missing"}
                  </td>
                  <td>
                    {row.reference_present
                      ? number(row.reference_quantity)
                      : "Missing"}
                  </td>
                  <td className={BigInt(row.difference) ? "difference" : ""}>
                    {BigInt(row.difference) > 0n ? "+" : ""}
                    {number(row.difference)}
                  </td>
                  <td>
                    {!row.observed_present
                      ? "Reference only"
                      : !row.reference_present
                        ? "Observed only"
                        : "Both snapshots"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          className={`notice ${d.status === "pass" ? "success" : "warning"}`}
        >
          No per-SKU differences were recorded. Review the result and manifest
          for the complete quality outcome.
        </div>
      )}
      <details>
        <summary>Comparison provenance</summary>
        <Definition
          values={[
            ["Run ID", <code>{d.run_id}</code>],
            ["Source ID", <code>{d.source_id}</code>],
            ["Snapshot ID", d.snapshot_id],
            ["Reference ID", d.reference_id],
            ["Observed SHA-256", <code>{d.observed_sha256}</code>],
            ["Reference SHA-256", <code>{d.reference_sha256}</code>],
            ["Manifest SHA-256", <code>{d.manifest_sha256}</code>],
          ]}
        />
        <pre>{json(d.manifest)}</pre>
      </details>
    </Panel>
  );
}
