import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."),
  OUT = path.resolve(ROOT, "../artifacts/dashboard/live");
await fs.mkdir(OUT, { recursive: true });
const origin = process.env.ORDERFLOW_URL ?? "http://127.0.0.1:8010";
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin))
  throw Error("Live checks are restricted to the owned local API.");
const files = [
  "src/App.tsx",
  "src/api.ts",
  "src/style.css",
  "src/types.ts",
  "src/main.tsx",
  "package.json",
  "package-lock.json",
  "index.html",
];
const hashes = async () =>
  Object.fromEntries(
    await Promise.all(
      files.map(async (f) => [
        f,
        crypto
          .createHash("sha256")
          .update(await fs.readFile(path.join(ROOT, f)))
          .digest("hex"),
      ]),
    ),
  );
const before = await hashes(),
  results = [],
  scans = [],
  errors = [],
  consoleErrors = [];
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  recordVideo: {
    dir: path.join(OUT, "video"),
    size: { width: 1440, height: 1000 },
  },
});
const page = await context.newPage();
page.setDefaultTimeout(15000);
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text());
});
const wait = async () =>
  page.waitForFunction(() => !document.querySelector(".loading"));
const go = async (name) => {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name, exact: true })
    .click();
  await wait();
};
const check = (ok, message) => {
  if (!ok) throw Error(message);
};
const run = async (name, fn) => {
  try {
    await fn();
    results.push({ name, passed: true });
    console.log(`PASS ${name}`);
  } catch (e) {
    results.push({ name, passed: false, error: e.stack });
    console.log(`FAIL ${name}: ${e.message}`);
    await page.screenshot({
      path: path.join(OUT, `failure-${results.length}.png`),
      fullPage: true,
    });
  }
};
const scan = async (state) => {
  const r = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  scans.push({
    state,
    violations: r.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.map((n) => n.target),
    })),
  });
  check(
    !r.violations.length,
    `Accessibility ${state}: ${r.violations.map((v) => v.id)}`,
  );
};
let originalOverview;
const observations = {};
try {
  await page.goto(origin);
  await page.getByRole("heading", { name: "Operations overview" }).waitFor();
  await wait();
  const login = await context.request.post(`${origin}/v1/dev/session`, {
    data: { identity_id: "alpha-operator" },
  });
  check(login.ok(), "Local fixture session unavailable");
  const auth = await login.json();
  const headers = {
    Authorization: `Bearer ${auth.access_token}`,
    "X-Tenant-ID": auth.principal.tenant_id,
  };
  const get = async (route) => {
    const r = await context.request.get(origin + route, { headers });
    check(r.ok(), `${route}: HTTP ${r.status()}`);
    return r.json();
  };
  originalOverview = await get("/v1/overview");
  await run(
    "Real PostgreSQL overview renders current snapshots and sources",
    async () => {
      check(
        originalOverview.counts.orders === 2,
        "Serving fixture order count changed",
      );
      check(
        originalOverview.order_totals.current_amount_minor === 8000,
        "Serving fixture current amount changed",
      );
      await page.getByText("$80.00", { exact: true }).first().waitFor();
      await page.screenshot({
        path: path.join(OUT, "01-overview-desktop.png"),
        fullPage: false,
      });
      await scan("1440 overview");
    },
  );
  await run("Real current order opens exact committed detail", async () => {
    await go("Current orders");
    const d = await get("/v1/current/orders?limit=25");
    check(d.items.length === 2, "Expected two serving orders");
    await page
      .getByRole("button", { name: d.items[0].entity_id, exact: true })
      .click();
    await page
      .getByRole("dialog")
      .getByText(d.items[0].payload.customer_id, { exact: true })
      .waitFor();
    await scan("order detail");
    await page.keyboard.press("Escape");
  });
  await run(
    "Real product history displays curated intervals and raw revisions",
    async () => {
      await go("Products & history");
      const d = await get("/v1/current/products?limit=25");
      check(d.items.length > 0, "No fixture products");
      const h = await get(
        `/v1/product-history?entity_id=${encodeURIComponent(d.items[0].entity_id)}`,
      );
      await page
        .getByRole("button", { name: d.items[0].entity_id, exact: true })
        .click();
      await page
        .getByRole("button", {
          name: `Raw revisions (${h.raw_revisions.length})`,
        })
        .waitFor();
      await page
        .getByRole("button", {
          name: `Raw revisions (${h.raw_revisions.length})`,
        })
        .click();
      await scan("product history");
      await page.screenshot({
        path: path.join(OUT, "02-product-history.png"),
        fullPage: false,
      });
      await page.keyboard.press("Escape");
    },
  );
  await run(
    "Real inbox filtering shows quarantined terminal outcomes",
    async () => {
      await go("Event inbox");
      await page
        .getByRole("combobox", { name: "Status", exact: true })
        .selectOption("quarantined");
      await wait();
      const d = await get("/v1/events?status=quarantined&limit=25");
      check(d.items.length === 2, "Expected two serving quarantines");
      await page
        .getByRole("button", { name: d.items[0].entity_id, exact: true })
        .first()
        .click();
      await page
        .getByText("This is a terminal quarantine.", { exact: false })
        .waitFor();
      check(
        (await page
          .getByRole("button", { name: "Request retry", exact: true })
          .count()) === 0,
        "Terminal retry must not appear",
      );
      await scan("terminal event");
      await page.keyboard.press("Escape");
    },
  );
  await run(
    "Real quarantine diagnostics and event drilldown remain inspectable",
    async () => {
      await go("Quarantines");
      const d = await get("/v1/quarantines?limit=25");
      await page
        .getByRole("button", { name: d.items[0].quarantine_id, exact: true })
        .click();
      await page
        .getByRole("heading", { name: "Redacted diagnostic details" })
        .waitFor();
      await scan("quarantine diagnostic");
      await page.screenshot({
        path: path.join(OUT, "03-quarantine-detail.png"),
        fullPage: false,
      });
      await page.keyboard.press("Escape");
    },
  );
  await run(
    "Real latest report accurately distinguishes success and no-success",
    async () => {
      await go("Runs & reports");
      const d = await get("/v1/reports/latest");
      observations.report = {
        has_success: d.has_success,
        stale: d.stale,
        latest_attempt_status: d.latest_attempt?.status ?? null,
      };
      if (d.has_success) {
        await page
          .getByRole("heading", { name: "Last successful publication" })
          .waitFor();
        if (d.stale)
          await page
            .getByText("Last successful publication is stale.", { exact: true })
            .waitFor();
      } else
        await page
          .getByRole("heading", { name: "No successful publication yet" })
          .waitFor();
      await scan("latest report");
      await page.screenshot({
        path: path.join(OUT, "04-reports.png"),
        fullPage: true,
      });
    },
  );
  await run(
    "Real inventory comparison exposes actual snapshot or explicit absence",
    async () => {
      await go("Inventory diff");
      const d = await get("/v1/inventory/latest");
      observations.inventory = {
        has_snapshot: d.has_snapshot,
        status: d.latest?.status ?? null,
      };
      if (d.has_snapshot)
        await page
          .getByRole("heading", { name: "Latest comparison result" })
          .waitFor();
      else
        await page
          .getByRole("heading", { name: "No inventory snapshot yet" })
          .waitFor();
      await scan("inventory");
      await page.screenshot({
        path: path.join(OUT, "05-inventory.png"),
        fullPage: true,
      });
    },
  );
  await run(
    "Real tenant and viewer transition clears prior workspace records",
    async () => {
      await page
        .getByRole("combobox", { name: "Local fixture identity" })
        .selectOption("beta-viewer");
      await page.waitForFunction((name) => {
        const e = document.querySelector(".tenant-name");
        return e && e.textContent !== name;
      }, auth.tenant.name);
      await go("Overview");
      await wait();
      const sessionText = await page.locator(".tenant-name").innerText();
      check(sessionText !== auth.tenant.name, "Tenant must change");
      await go("Current orders");
      const betaLogin = await context.request.post(`${origin}/v1/dev/session`, {
        data: { identity_id: "beta-viewer" },
      });
      check(betaLogin.ok(), "Independent Beta viewer session verifies");
      const beta = await betaLogin.json();
      const betaResponse = await context.request.get(
        `${origin}/v1/current/orders?limit=25`,
        {
          headers: {
            Authorization: `Bearer ${beta.access_token}`,
            "X-Tenant-ID": beta.principal.tenant_id,
          },
        },
      );
      check(
        betaResponse.ok(),
        "Independent Beta current-order oracle responds",
      );
      const betaRows = (await betaResponse.json()).items;
      const uiRows = await page
        .locator(".table-wrap tbody tr")
        .allTextContents();
      check(
        uiRows.length === betaRows.length,
        "Rendered row count matches the verified Beta tenant, not the previous Alpha view",
      );
      for (const order of betaRows)
        check(
          uiRows.some(
            (row) =>
              row.includes(order.entity_id) &&
              row.includes(order.payload.customer_id) &&
              row.includes(order.payload.status),
          ),
          "Rendered entity/customer/status tuple matches independently scoped Beta records, even when tenant IDs collide",
        );
      check(
        await page.evaluate(
          () => localStorage.length === 0 && sessionStorage.length === 0,
        ),
        "No token storage",
      );
      await scan("beta viewer");
      await page
        .getByRole("combobox", { name: "Local fixture identity" })
        .selectOption("alpha-operator");
      await page.waitForFunction(
        (name) => document.querySelector(".tenant-name")?.textContent === name,
        auth.tenant.name,
      );
      await go("Overview");
      await wait();
    },
  );
  await run(
    "Real responsive layouts and keyboard access at 375 and 768",
    async () => {
      for (const width of [375, 768]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const name of [
          "Overview",
          "Current orders",
          "Products & history",
          "Event inbox",
          "Quarantines",
          "Runs & reports",
          "Inventory diff",
        ]) {
          await go(name);
          check(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth + 1,
            ),
            `Page overflow ${width} ${name}`,
          );
          await scan(`${width} ${name}`);
          if (
            width === 375 &&
            ["Overview", "Products & history", "Inventory diff"].includes(name)
          )
            await page.screenshot({
              path: path.join(
                OUT,
                `mobile-${name.split(" ")[0].toLowerCase()}.png`,
              ),
              fullPage: true,
            });
        }
      }
      await page.setViewportSize({ width: 1440, height: 1000 });
      await go("Overview");
      await page.evaluate(() => {
        document.activeElement?.blur();
        scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(OUT, "06-clean-overview-viewport.png"),
        fullPage: false,
      });
    },
  );
  await run(
    "Serving fixture state is unchanged by read-only browser journeys",
    async () => {
      const after = await get("/v1/overview");
      check(
        JSON.stringify(after.counts) ===
          JSON.stringify(originalOverview.counts),
        "Serving counts changed",
      );
      check(
        JSON.stringify(after.order_totals) ===
          JSON.stringify(originalOverview.order_totals),
        "Serving order totals changed",
      );
      check(!errors.length, `Page errors: ${errors.join("; ")}`);
    },
  );
} catch (e) {
  results.push({ name: "live setup", passed: false, error: e.stack });
} finally {
  const after = await hashes();
  const video = page.video()
    ? path.relative(OUT, await page.video().path()).replaceAll("\\", "/")
    : null;
  await context.close();
  await browser.close();
  const report = {
    recorded_at: new Date().toISOString(),
    origin,
    mode: "actual HTTP API and PostgreSQL; read-only domain operations; fixture sessions only",
    passed: results.filter((r) => r.passed).length,
    total: results.length,
    results,
    observations,
    video,
    accessibility: scans,
    pageerrors: errors,
    console_errors: consoleErrors,
    source_hashes: before,
    source_stable: JSON.stringify(before) === JSON.stringify(after),
  };
  await fs.writeFile(
    path.join(OUT, "live-results.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(
    JSON.stringify({
      passed: report.passed,
      total: report.total,
      a11y_states: scans.length,
      violations: scans.reduce((n, s) => n + s.violations.length, 0),
      source_stable: report.source_stable,
      pageerrors: errors,
    }),
  );
  if (report.passed !== report.total || !report.source_stable)
    process.exitCode = 1;
}
