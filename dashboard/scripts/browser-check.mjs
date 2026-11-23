import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.resolve(
  process.env.ORDERFLOW_BROWSER_OUT ??
    path.join(ROOT, "../artifacts/dashboard"),
);
const DIST = path.resolve(
  process.env.ORDERFLOW_BROWSER_DIST ?? path.join(ROOT, "dist"),
);
await fs.mkdir(OUT, { recursive: true });
const sourceFiles = [
  "src/App.tsx",
  "src/auth.ts",
  "src/AuthForm.tsx",
  "src/api.ts",
  "src/types.ts",
  "src/style.css",
  "src/main.tsx",
  "package.json",
  "package-lock.json",
  "index.html",
];
const hashes = async () =>
  Object.fromEntries(
    await Promise.all(
      sourceFiles.map(async (p) => [
        p,
        crypto
          .createHash("sha256")
          .update(await fs.readFile(path.join(ROOT, p)))
          .digest("hex"),
      ]),
    ),
  );
const before = await hashes();
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost"),
      relative =
        url.pathname === "/"
          ? "index.html"
          : decodeURIComponent(url.pathname.slice(1)),
      target = path.resolve(DIST, relative);
    if (!target.startsWith(DIST + path.sep)) {
      res.writeHead(404);
      res.end();
      return;
    }
    const body = await fs.readFile(target);
    res.writeHead(200, {
      "Content-Type":
        {
          ".html": "text/html",
          ".js": "text/javascript",
          ".css": "text/css",
          ".svg": "image/svg+xml",
        }[path.extname(target)] ?? "application/octet-stream",
      "Content-Security-Policy":
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
    });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end();
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ channel: "chrome", headless: true });
const timestamp = "2026-11-23T12:00:00Z",
  sourceId = "11111111-1111-4111-8111-111111111111";
const tenants = {
  alpha: {
    tenant_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    name: "Alpha Retail",
    currency: "USD",
  },
  beta: {
    tenant_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    name: "Beta Retail",
    currency: "USD",
  },
};
const identities = Object.entries(tenants).flatMap(([tenant, t]) =>
  ["operator", "viewer"].map((role) => ({
    id: `${tenant}-${role}`,
    label: `${t.name} · ${role}`,
    tenant_id: t.tenant_id,
    role,
  })),
);
const source = {
  source_id: sourceId,
  name: "Orders event stream",
  connector_type: "events",
  enabled: true,
  authoritative_entity_types: ["order"],
  accepted_count: 35,
  completed_count: 31,
  pending_count: 3,
  retry_wait_count: 1,
  quarantine_count: 1,
  oldest_pending_at: timestamp,
  last_commit_at: timestamp,
  last_successful_interval_end: timestamp,
};
const csv = {
  ...source,
  source_id: "22222222-2222-4222-8222-222222222222",
  name: "Warehouse CSV",
  connector_type: "csv",
  authoritative_entity_types: [],
  pending_count: 0,
  oldest_pending_at: null,
};
const event = (id, status = "completed") => ({
  event_id: id,
  source_id: sourceId,
  source_event_id: `source-${id}`,
  entity_type: "order",
  entity_id: `order-${id}`,
  entity_version: 3,
  status,
  attempt_count: 1,
  ingested_at: timestamp,
  effective_at: timestamp,
  occurred_at: timestamp,
  available_at: timestamp,
  outcome: status === "completed" ? "applied_current" : null,
  reason_code: status === "quarantined" ? "currency_mismatch" : null,
  envelope_sha256: "a".repeat(64),
  payload_sha256: "b".repeat(64),
  committed_at: status === "completed" ? timestamp : null,
});
const run = {
  run_id: "run-latest",
  source_id: sourceId,
  status: "failed",
  interval_start: timestamp,
  interval_end: timestamp,
  started_at: timestamp,
  finished_at: timestamp,
  failure_code: "quality_failed",
  checks: [
    {
      name: "order_total_matches",
      status: "fail",
      observed: 8000,
      expected: 8100,
    },
  ],
};
const goodRun = {
  ...run,
  run_id: "run-good",
  status: "published",
  failure_code: null,
  checks: [{ name: "unique_orders", status: "pass", observed: 2, expected: 2 }],
};
const report = {
  has_success: true,
  stale: true,
  observed_at: timestamp,
  last_good: {
    run_id: "run-good",
    source_id: sourceId,
    interval_start: timestamp,
    interval_end: timestamp,
    published_at: timestamp,
    manifest_sha256: "c".repeat(64),
    storage: "local",
    artifact_availability: "local_only",
    summary: {
      order_count: 2,
      order_total_minor: 8000,
      currency: "USD",
      product_count: 2,
      raw_revision_count: 5,
    },
    order_facts: [
      {
        entity_id: "O1",
        entity_version: 2,
        total_minor: 8000,
        currency: "USD",
        status: "paid",
      },
    ],
    product_history: [
      {
        entity_id: "..",
        entity_version: 3,
        valid_from: timestamp,
        valid_to: null,
        unit_price_minor: 2500,
        currency: "USD",
      },
    ],
    preview_limit: 100,
    preview_truncated: true,
    manifest: { storage: "local", row_count: 101 },
  },
  latest_attempt: run,
};
const inventory = {
  run_id: "inventory-1",
  source_id: csv.source_id,
  snapshot_id: "WH-2026-11-23",
  reference_id: "oracle-23",
  as_of: timestamp,
  status: "fail",
  observed_sha256: "d".repeat(64),
  reference_sha256: "e".repeat(64),
  observed_total: 20,
  reference_total: 20,
  difference_total: 0,
  differences: [
    {
      sku: "SKU-A",
      observed_quantity: 12,
      reference_quantity: 10,
      difference: 2,
      observed_present: true,
      reference_present: true,
    },
    {
      sku: "SKU-B",
      observed_quantity: 8,
      reference_quantity: 10,
      difference: -2,
      observed_present: true,
      reference_present: true,
    },
    {
      sku: "SKU-C",
      observed_quantity: 0,
      reference_quantity: 0,
      difference: 0,
      observed_present: false,
      reference_present: true,
    },
  ],
  manifest_sha256: "f".repeat(64),
  manifest: { comparison: "independent" },
  storage: "local",
  artifact_availability: "local_only",
};
const entity = (id, kind = "orders") => ({
  entity_id: id,
  entity_version: "9223372036854775807",
  source_id: sourceId,
  event_id: "event-entity",
  effective_at: timestamp,
  payload:
    kind === "orders"
      ? {
          customer_id: "synthetic-customer",
          total_minor: 12345,
          currency: "USD",
          status: "paid",
        }
      : {
          sku: "SKU-HISTORY",
          name: "Trail flask <script>text only</script>",
          unit_price_minor: 2500,
          currency: "USD",
        },
});
function overview(tenant = "alpha") {
  return {
    tenant: tenants[tenant],
    observed_at: timestamp,
    counts: {
      accepted: 35,
      pending: 3,
      retry_wait: 1,
      completed: 31,
      quarantined: 1,
      revisions: 31,
      products: 2,
      orders: 2,
    },
    order_totals: {
      current_amount_minor: tenant === "beta" ? 7777 : "900719925474099312",
      paid_amount_minor: tenant === "beta" ? 7777 : 8000,
      currency: "USD",
    },
    freshness: {
      oldest_pending_at: null,
      last_commit_at: timestamp,
      last_successful_interval_end: timestamp,
    },
    sources: [source, csv],
    recent_runs: [run, goodRun],
  };
}
function history(id) {
  return {
    entity_id: id,
    raw_revisions: [1, 3, 2].map((v, i) => ({
      entity_version: v,
      effective_at: `2026-11-${20 + i}T12:00:00Z`,
      event_id: `revision-${v}`,
      payload: {
        sku: "SKU-HISTORY",
        name: "Trail flask",
        unit_price_minor: 1500 + v * 500,
        currency: "USD",
      },
    })),
    intervals: [1, 2, 3].map((v, i) => ({
      entity_version: v,
      valid_from: `2026-11-${20 + i}T12:00:00Z`,
      valid_to: i === 2 ? null : `2026-11-${21 + i}T12:00:00Z`,
      payload: {
        sku: "SKU-HISTORY",
        name: "Trail flask",
        unit_price_minor: 1500 + v * 500,
        currency: "USD",
      },
    })),
  };
}
const results = [],
  a11y = [];
export async function scenario(name, fn, options = {}) {
  const context = await browser.newContext({
      viewport: options.viewport ?? { width: 1440, height: 1000 },
    }),
    page = await context.newPage(),
    errors = [],
    requests = [],
    overrides = [];
  page.setDefaultTimeout(10000);
  let eventState = "retry_wait",
    fixture = options.fixture !== false;
  page.on("pageerror", (e) => errors.push(e.message));
  const respond = async (route, body, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body)
        .replaceAll('"9223372036854775807"', "9223372036854775807")
        .replaceAll('"900719925474099312"', "900719925474099312"),
    });
  await page.route("**/v1/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      p = url.pathname,
      auth = req.headers().authorization ?? "",
      identity = auth.replace("Bearer token-", ""),
      [tenant = "alpha", role = "operator"] = identity.split("-");
    requests.push({
      path: p,
      query: url.search,
      method: req.method(),
      identity,
      body: req.postData(),
    });
    for (const handler of overrides)
      if (await handler({ route, request: req, url, tenant, role, respond }))
        return;
    if (p === "/v1/auth/config")
      return respond(
        route,
        options.authConfig ?? {
          mode: fixture ? "fixture" : "bearer",
          fixture_login_enabled: fixture,
          identities: fixture ? identities : [],
        },
      );
    if (p === "/v1/dev/session") {
      const id = JSON.parse(req.postData()).identity_id,
        [t, r] = id.split("-");
      return respond(route, {
        access_token: `token-${id}`,
        token_type: "bearer",
        principal: { tenant_id: tenants[t].tenant_id, subject: id, role: r },
        tenant: tenants[t],
        demo: true,
      });
    }
    if (p === "/v1/session")
      return respond(route, {
        principal: {
          tenant_id: tenants[tenant]?.tenant_id ?? tenants.alpha.tenant_id,
          subject: identity,
          role,
        },
        tenant: tenants[tenant] ?? tenants.alpha,
      });
    if (p === "/v1/overview") return respond(route, overview(tenant));
    if (p === "/v1/sources") return respond(route, { items: [source, csv] });
    if (p.startsWith("/v1/current/")) {
      const kind = p.endsWith("orders") ? "orders" : "products";
      return respond(route, {
        items:
          kind === "products"
            ? [entity("..", kind), entity("P/二", kind)]
            : [entity("O1")],
        next_cursor: null,
      });
    }
    if (p === "/v1/product-history")
      return respond(route, history(url.searchParams.get("entity_id")));
    if (p === "/v1/events") {
      const status = url.searchParams.get("status");
      return respond(route, {
        items: status
          ? [event("filter-event", status)]
          : url.searchParams.has("cursor")
            ? [event("second-page")]
            : [
                event("retry-event", eventState),
                event("terminal", "quarantined"),
                ...Array.from({ length: 23 }, (_, i) => event(`event-${i}`)),
              ],
        next_cursor:
          !status && !url.searchParams.has("cursor") ? "opaque-cursor" : null,
      });
    }
    if (p === "/v1/events/retry-event/retry") {
      eventState = "pending";
      return respond(
        route,
        { event_id: "retry-event", status: "pending" },
        202,
      );
    }
    if (p.startsWith("/v1/events/"))
      return respond(
        route,
        event(
          p.split("/")[3],
          p.endsWith("retry-event") ? eventState : "quarantined",
        ),
      );
    if (p === "/v1/quarantines")
      return respond(route, {
        items: [
          {
            quarantine_id: "quarantine-1",
            event_id: "terminal",
            reason_code: "currency_mismatch",
            created_at: timestamp,
            redacted_details: {
              field: "currency",
              message: "<img src=x onerror=alert(1)> is inert text",
            },
          },
        ],
        next_cursor: null,
      });
    if (p === "/v1/reports/latest") return respond(route, report);
    if (p.startsWith("/v1/runs/"))
      return respond(route, p.endsWith("run-good") ? goodRun : run);
    if (p === "/v1/inventory/latest")
      return respond(route, { has_snapshot: true, latest: inventory });
    if (p === "/v1/inventory/history")
      return respond(route, { items: [inventory], truncated: false });
    return respond(
      route,
      { code: "unexpected_mock_request", message: p, request_id: "mock-404" },
      404,
    );
  });
  const go = async (view) => {
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: view, exact: true })
      .click();
  };
  const axe = async (state) => {
    const r = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    a11y.push({
      scenario: name,
      state,
      incomplete: r.incomplete.map((rule) => ({
        id: rule.id,
        impact: rule.impact,
        help: rule.help,
        nodes: rule.nodes.map((node) => ({
          target: node.target,
          summary: node.failureSummary,
        })),
      })),
      violations: r.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        nodes: v.nodes.map((n) => n.target),
      })),
    });
    if (r.violations.length)
      throw Error(`Accessibility: ${r.violations.map((v) => v.id).join(", ")}`);
  };
  const check = async (condition, message) => {
    if (!(await condition)) {
      if (message.includes("overflow"))
        console.log(
          JSON.stringify(
            await page.evaluate(() => ({
              width: innerWidth,
              scrollWidth: document.documentElement.scrollWidth,
              nodes: [...document.querySelectorAll("body *")]
                .filter(
                  (e) =>
                    e.getBoundingClientRect().right > innerWidth + 1 &&
                    getComputedStyle(e).position !== "absolute",
                )
                .slice(0, 25)
                .map((e) => ({
                  tag: e.tagName,
                  cls: e.className,
                  right: e.getBoundingClientRect().right,
                  width: e.getBoundingClientRect().width,
                  overflow: getComputedStyle(e).overflow,
                })),
            })),
          ),
        );
      throw Error(message);
    }
  };
  try {
    await options.prepare?.({
      page,
      override: (handler) => overrides.unshift(handler),
      respond,
    });
    await page.goto(origin + (options.urlSuffix || ""));
    if (fixture)
      await page
        .getByRole("heading", { name: "Operations overview" })
        .waitFor();
    await fn({
      page,
      go,
      axe,
      check,
      requests,
      override: (handler) => overrides.unshift(handler),
      respond,
      setEventState: (value) => (eventState = value),
    });
    if (errors.length) throw Error(errors.join("; "));
    results.push({ name, passed: true, pageerrors: errors });
    console.log(`PASS ${name}`);
  } catch (e) {
    results.push({ name, passed: false, error: e.stack, pageerrors: errors });
    console.log(`FAIL ${name}: ${e.message}`);
    await page.screenshot({
      path: path.join(OUT, `${results.length}-failure.png`),
      fullPage: true,
    });
  }
  await context.close();
}
export { ROOT, OUT, sourceId, history, event, entity, overview };
export async function finishHarness(expected = 18) {
  const after = await hashes();
  const reportOut = {
    recorded_at: new Date().toISOString(),
    mode: "built React app; intercepted API; isolated synthetic contexts",
    passed: results.filter((r) => r.passed).length,
    total: results.length,
    results,
    accessibility: a11y,
    source_hashes: before,
    source_stable: JSON.stringify(before) === JSON.stringify(after),
  };
  await fs.writeFile(
    path.join(OUT, "browser-results.json"),
    JSON.stringify(reportOut, null, 2),
  );
  console.log(
    JSON.stringify(
      {
        passed: reportOut.passed,
        total: reportOut.total,
        a11y_states: a11y.length,
        a11y_violations: a11y.reduce((n, a) => n + a.violations.length, 0),
        source_stable: reportOut.source_stable,
        failures: results
          .filter((r) => !r.passed)
          .map((r) => ({ name: r.name, error: r.error })),
      },
      null,
      2,
    ),
  );
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  if (
    reportOut.passed !== reportOut.total ||
    reportOut.total !== expected ||
    !reportOut.source_stable
  )
    process.exitCode = 1;
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    await scenario(
      "Overview uses exact aggregate amounts and source ledger",
      async ({ page, axe, check }) => {
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
        await check(
          page
            .getByText("No pending backlog", { exact: true })
            .count()
            .then((n) => n > 0),
          "Null backlog is explicit",
        );
        await page.screenshot({
          path: path.join(OUT, "01-overview-desktop.png"),
          fullPage: true,
        });
        await axe("overview");
      },
    );
    await scenario(
      "Current order detail preserves maximum bigint version",
      async ({ page, go, check, axe }) => {
        await go("Current orders");
        await page.getByRole("button", { name: "O1", exact: true }).click();
        await page
          .getByRole("dialog")
          .getByText("9223372036854775807", { exact: true })
          .waitFor();
        await check(
          page
            .getByRole("dialog")
            .getByText("$123.45", { exact: true })
            .isVisible(),
          "Exact minor-unit price",
        );
        await axe("order detail");
        await page.keyboard.press("Escape");
        await page.getByRole("heading", { name: "Order snapshots" }).waitFor();
      },
    );
    await scenario(
      "History query preserves dot and slash identifiers; real interval chart",
      async ({ page, go, requests, check, axe }) => {
        await go("Products & history");
        await page.getByRole("button", { name: "..", exact: true }).click();
        await page
          .getByRole("dialog")
          .getByRole("img", { name: /Effective price across 3/ })
          .waitFor();
        await check(
          Promise.resolve(
            requests.some(
              (r) =>
                r.path === "/v1/product-history" && r.query === "?entity_id=..",
            ),
          ),
          "Dot ID must use query form",
        );
        await page.getByRole("button", { name: "Raw revisions (3)" }).click();
        await page.getByText("revision-2", { exact: true }).waitFor();
        await axe("history raw revisions");
        await page.keyboard.press("Escape");
        await page.getByRole("button", { name: "P/二", exact: true }).click();
        await page
          .getByRole("dialog")
          .getByText("Open end", { exact: true })
          .waitFor();
        await check(
          Promise.resolve(
            requests.some((r) => r.query.includes("P%2F%E4%BA%8C")),
          ),
          "Slash Unicode ID preserved",
        );
      },
    );
    await scenario(
      "Inbox keyset paging and filters reset page cursor",
      async ({ page, go, requests, check, axe }) => {
        await go("Event inbox");
        await page.getByRole("button", { name: "Next page" }).click();
        await page
          .getByRole("button", { name: "order-second-page", exact: true })
          .waitFor();
        await page
          .getByLabel("Status", { exact: true })
          .selectOption("quarantined");
        await page
          .getByRole("button", { name: "order-filter-event" })
          .waitFor();
        await check(
          Promise.resolve(
            requests
              .filter((r) => r.path === "/v1/events")
              .at(-1)
              .query.includes("status=quarantined") &&
              !requests
                .filter((r) => r.path === "/v1/events")
                .at(-1)
                .query.includes("cursor="),
          ),
          "Filter reset cursor",
        );
        await check(
          page
            .getByRole("button", { name: "Previous", exact: true })
            .isDisabled(),
          "Previous disabled on reset",
        );
        await axe("filtered inbox");
      },
    );
    await scenario(
      "Operator retry verifies resulting state and submits bounded reason",
      async ({ page, go, requests, check, axe }) => {
        await go("Event inbox");
        await page
          .getByRole("button", { name: "order-retry-event", exact: true })
          .click();
        await page
          .getByLabel("Operational reason")
          .fill("Reviewed upstream transient outage.");
        await axe("retry form");
        await page
          .getByRole("button", { name: "Request retry", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("Pending", { exact: true })
          .waitFor();
        await check(
          page
            .getByRole("button", { name: "Request retry", exact: true })
            .count()
            .then((n) => n === 0),
          "No duplicate retry once pending",
        );
        await check(
          Promise.resolve(
            requests.filter(
              (r) => r.method === "POST" && r.path.endsWith("/retry"),
            ).length === 1,
          ),
          "One retry mutation",
        );
      },
    );
    await scenario(
      "Viewer can inspect retry-wait without mutation controls",
      async ({ page, go, check }) => {
        await page
          .getByRole("combobox", { name: "Local fixture identity" })
          .selectOption("alpha-viewer");
        await page
          .getByRole("heading", { name: "Operations overview" })
          .waitFor();
        await go("Event inbox");
        await page
          .getByRole("button", { name: "order-retry-event", exact: true })
          .click();
        await page
          .getByText("An operator must request a retry.", { exact: false })
          .waitFor();
        await check(
          page
            .getByRole("button", { name: "Request retry", exact: true })
            .count()
            .then((n) => n === 0),
          "Viewer has no retry action",
        );
      },
    );
    await scenario(
      "Quarantine diagnostic text is inert and terminal correction is explicit",
      async ({ page, go, axe, check }) => {
        await go("Quarantines");
        await page
          .getByRole("button", { name: "quarantine-1", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText(/is inert text/)
          .waitFor();
        await check(
          page
            .locator("dialog img")
            .count()
            .then((n) => n === 0),
          "Diagnostic markup must not create elements",
        );
        await axe("quarantine detail");
        await page.keyboard.press("Escape");
        await page.getByRole("button", { name: "Inspect event" }).click();
        await page
          .getByText("This is a terminal quarantine.", { exact: false })
          .waitFor();
        await check(
          page
            .getByRole("button", { name: "Request retry", exact: true })
            .count()
            .then((n) => n === 0),
          "No retry for terminal event",
        );
      },
    );
    await scenario(
      "Failed latest report preserves last-good, previews, and run checks",
      async ({ page, go, axe }) => {
        await go("Runs & reports");
        await page
          .getByText("Last successful publication is stale.", { exact: true })
          .waitFor();
        await page
          .getByRole("heading", { name: "Last successful publication" })
          .waitFor();
        await page
          .getByRole("button", { name: "Product intervals", exact: true })
          .click();
        await page.getByText("Open end", { exact: true }).waitFor();
        await page
          .getByRole("button", { name: "Inspect run", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("Order total matches", { exact: true })
          .waitFor();
        await axe("run checks");
        await page.screenshot({
          path: path.join(OUT, "02-report-checks.png"),
          fullPage: false,
        });
      },
    );
    await scenario(
      "Inventory net-zero still reveals per-SKU and missing-row differences",
      async ({ page, go, axe, check }) => {
        await go("Inventory diff");
        await page.getByText("Reference only", { exact: true }).waitFor();
        await check(
          page.getByText("+2", { exact: true }).isVisible(),
          "Positive difference",
        );
        await check(
          page.getByText("-2", { exact: true }).isVisible(),
          "Negative difference",
        );
        await axe("inventory diff");
        await page.screenshot({
          path: path.join(OUT, "03-inventory-diff.png"),
          fullPage: true,
        });
        await page
          .getByRole("button", { name: "WH-2026-11-23", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("Reference only", { exact: true })
          .waitFor();
      },
    );
    await scenario(
      "Late previous-tenant reads cannot repopulate switched workspace",
      async ({ page, override, respond, check }) => {
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
        let release, started;
        const began = new Promise((r) => (started = r));
        override(async ({ route, url, tenant }) => {
          if (url.pathname === "/v1/overview" && tenant === "alpha") {
            started();
            await new Promise((r) => (release = r));
            try {
              await respond(route, {
                ...overview("alpha"),
                order_totals: {
                  current_amount_minor: 11111101,
                  paid_amount_minor: 11111101,
                  currency: "USD",
                },
              });
            } catch {}
            return true;
          }
          return false;
        });
        await page.getByRole("button", { name: "Refresh workspace" }).click();
        await began;
        await page
          .getByRole("combobox", { name: "Local fixture identity" })
          .selectOption("beta-operator");
        await page.getByText("Beta Retail", { exact: true }).first().waitFor();
        await page.getByText("$77.77", { exact: true }).first().waitFor();
        release();
        await page.waitForTimeout(150);
        await check(
          page
            .getByText("$111,111.01", { exact: true })
            .count()
            .then((n) => n === 0),
          "Old tenant's rendered amount cannot reappear",
        );
        await check(
          page.getByText("$77.77", { exact: true }).first().isVisible(),
          "Verified Beta amount remains rendered",
        );
        await check(
          page.evaluate(
            () => localStorage.length === 0 && sessionStorage.length === 0,
          ),
          "Tokens not stored",
        );
      },
    );
    await scenario(
      "Closed late product detail cannot overwrite the next product",
      async ({ page, go, override, respond, check }) => {
        let release;
        override(async ({ route, url }) => {
          if (
            url.pathname === "/v1/product-history" &&
            url.searchParams.get("entity_id") === ".."
          ) {
            await new Promise((r) => (release = r));
            try {
              const stale = history("..");
              for (const revision of [
                ...stale.raw_revisions,
                ...stale.intervals,
              ])
                revision.payload = {
                  ...revision.payload,
                  name: "STALE PRIVATE PRODUCT",
                  sku: "STALE-PRIVATE-SKU",
                  unit_price_minor: 88888,
                };
              await respond(route, stale);
            } catch {}
            return true;
          }
          return false;
        });
        await go("Products & history");
        await page.getByRole("button", { name: "..", exact: true }).click();
        await page.getByRole("dialog").waitFor();
        await page.getByRole("button", { name: "Close detail" }).click();
        await page.getByRole("button", { name: "P/二", exact: true }).click();
        await page
          .getByRole("dialog")
          .getByText("Open end", { exact: true })
          .waitFor();
        release();
        await page.waitForTimeout(100);
        await check(
          page
            .getByText("STALE-PRIVATE-SKU", { exact: true })
            .count()
            .then((n) => n === 0),
          "Late closed modal response suppressed",
        );
        await check(
          page
            .getByRole("dialog")
            .getByRole("button", { name: "Raw revisions (3)" })
            .isVisible(),
          "Current product's own history remains rendered",
        );
      },
    );
    await scenario(
      "Refresh failure retains labelled prior data and request ID",
      async ({ page, override, respond }) => {
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
        override(async ({ route, url }) => {
          if (url.pathname === "/v1/overview") {
            await respond(
              route,
              {
                code: "db_unavailable",
                message: "The database is temporarily unavailable.",
                request_id: "review-request-503",
              },
              503,
            );
            return true;
          }
          return false;
        });
        await page.getByRole("button", { name: "Refresh workspace" }).click();
        await page
          .getByText(
            "Showing the last successfully loaded view. It may be out of date.",
            { exact: true },
          )
          .waitFor();
        await page.getByText("review-request-503", { exact: true }).waitFor();
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
      },
    );
    await scenario(
      "Unauthorized background read clears records and credentials",
      async ({ page, override, respond, check }) => {
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
        override(async ({ route, url }) => {
          if (url.pathname === "/v1/overview") {
            await respond(
              route,
              {
                code: "invalid_token",
                message: "Expired",
                request_id: "401-review",
              },
              401,
            );
            return true;
          }
          return false;
        });
        await page.getByRole("button", { name: "Refresh workspace" }).click();
        await page
          .getByRole("heading", { name: "Connect to OrderFlow" })
          .waitFor();
        await check(
          page
            .getByText("$9,007,199,254,740,993.12", { exact: true })
            .count()
            .then((n) => n === 0),
          "Protected records removed",
        );
        await check(
          page
            .getByText("Your session expired. Connect again to continue.", {
              exact: true,
            })
            .isVisible(),
          "Session explanation",
        );
      },
    );
    await scenario(
      "Lost retry acknowledgement verifies state before another attempt",
      async ({ page, go, override, setEventState, check, requests }) => {
        override(async ({ route, url, request }) => {
          if (url.pathname.endsWith("/retry") && request.method() === "POST") {
            setEventState("pending");
            await route.abort("failed");
            return true;
          }
          return false;
        });
        await go("Event inbox");
        await page
          .getByRole("button", { name: "order-retry-event", exact: true })
          .click();
        await page
          .getByLabel("Operational reason")
          .fill("Reviewed after worker connection recovery.");
        await page
          .getByRole("button", { name: "Request retry", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("Pending", { exact: true })
          .waitFor();
        await check(
          page
            .getByRole("button", { name: "Request retry", exact: true })
            .count()
            .then((n) => n === 0),
          "Pending read suppresses duplicate mutation",
        );
        await check(
          Promise.resolve(
            requests.filter(
              (r) => r.method === "POST" && r.path.endsWith("/retry"),
            ).length === 1,
          ),
          "Single lost-ack mutation",
        );
      },
    );
    await scenario(
      "Bearer deployment offers no fixture login and stores no token",
      async ({ page, check }) => {
        await page
          .getByRole("heading", { name: "Connect to OrderFlow" })
          .waitFor();
        await check(
          page
            .getByRole("combobox", { name: "Local fixture identity" })
            .count()
            .then((n) => n === 0),
          "Production has no fixture picker",
        );
        await page
          .getByLabel("Access token", { exact: true })
          .fill("token-alpha-viewer");
        await page.getByRole("button", { name: "Verify and connect" }).click();
        await page
          .getByRole("heading", { name: "Operations overview" })
          .waitFor();
        await check(
          page.evaluate(
            () => localStorage.length === 0 && sessionStorage.length === 0,
          ),
          "No browser storage",
        );
        await page.reload();
        await page
          .getByRole("heading", { name: "Connect to OrderFlow" })
          .waitFor();
        await check(
          page
            .getByLabel("Access token", { exact: true })
            .inputValue()
            .then((v) => v === ""),
          "Token not restored on reload",
        );
      },
      { fixture: false },
    );
    await scenario(
      "No-success and empty views never manufacture healthy results",
      async ({ page, go, override, respond, axe }) => {
        override(async ({ route, url }) => {
          if (url.pathname === "/v1/reports/latest") {
            await respond(route, {
              has_success: false,
              stale: false,
              observed_at: timestamp,
              last_good: null,
              latest_attempt: run,
            });
            return true;
          }
          if (url.pathname === "/v1/inventory/latest") {
            await respond(route, { has_snapshot: false, latest: null });
            return true;
          }
          if (
            url.pathname === "/v1/inventory/history" ||
            url.pathname === "/v1/quarantines"
          ) {
            await respond(route, {
              items: [],
              truncated: false,
              next_cursor: null,
            });
            return true;
          }
          return false;
        });
        await go("Runs & reports");
        await page
          .getByRole("heading", { name: "No successful publication yet" })
          .waitFor();
        await axe("no last-good publication");
        await go("Inventory diff");
        await page
          .getByRole("heading", { name: "No inventory snapshot yet" })
          .waitFor();
        await axe("no inventory snapshot");
        await go("Quarantines");
        await page
          .getByRole("heading", { name: "No quarantines", exact: true })
          .waitFor();
        await axe("empty quarantines");
      },
    );
    await scenario(
      "Hung read times out, preserves prior view, and allows recovery",
      async ({ page, override, respond }) => {
        await page
          .getByText("$9,007,199,254,740,993.12", { exact: true })
          .first()
          .waitFor();
        let block = true;
        override(async ({ route, url }) => {
          if (url.pathname === "/v1/overview" && block) {
            await new Promise((resolve) => setTimeout(resolve, 21_000));
            try {
              await respond(route, overview());
            } catch {
              /* The real request deadline cancelled this route. */
            }
            return true;
          }
          return false;
        });
        await page.getByRole("button", { name: "Refresh workspace" }).click();
        await page
          .getByText(
            "The service did not respond within 20 seconds. Refresh to verify the latest state.",
            { exact: true },
          )
          .waitFor({ timeout: 25_000 });
        await page
          .getByText(
            "Showing the last successfully loaded view. It may be out of date.",
            { exact: true },
          )
          .waitFor();
        block = false;
        await page
          .getByRole("button", { name: "Try again", exact: true })
          .click();
        await page.getByRole("alert").waitFor({ state: "hidden" });
      },
    );
    await scenario(
      "Responsive keyboard workspace 375 and 768 has no page overflow",
      async ({ page, go, axe, check }) => {
        for (const width of [375, 768]) {
          await page.setViewportSize({ width, height: 900 });
          for (const [name, file] of [
            ["Overview", "overview"],
            ["Current orders", "orders"],
            ["Products & history", "products"],
            ["Event inbox", "events"],
            ["Quarantines", "quarantine"],
            ["Runs & reports", "reports"],
            ["Inventory diff", "inventory"],
          ]) {
            await go(name);
            await page
              .locator(".loading")
              .waitFor({ state: "hidden" })
              .catch(() => {});
            await check(
              page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth + 1,
              ),
              `No viewport overflow ${width}/${name}`,
            );
            await axe(`${width} ${name}`);
            if (
              width === 375 &&
              ["overview", "products", "inventory"].includes(file)
            )
              await page.screenshot({
                path: path.join(OUT, `mobile-${file}.png`),
                fullPage: true,
              });
          }
          await page.keyboard.press("Tab");
          await check(
            page.evaluate(() => document.activeElement !== document.body),
            "Keyboard focus retained",
          );
        }
      },
    );
  } finally {
    await finishHarness();
  }
}
