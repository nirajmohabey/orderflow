/** Regression checks against the built dashboard. Every API response is intercepted.
 * No serving API, database, external identity provider or paid service is used.
 * ORDERFLOW_BROWSER_OUT and ORDERFLOW_BROWSER_DIST support owned review evidence.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
process.env.ORDERFLOW_BROWSER_OUT ??= path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../artifacts/dashboard/regressions",
);
const { scenario, finishHarness } = await import("./browser-check.mjs");
try {
  await scenario(
    "fresh: Back to the original root URL restores Overview",
    async ({ page, go, check }) => {
      await go("Current orders");
      await page
        .getByRole("heading", { name: "Current orders", exact: true })
        .waitFor();
      await page.goBack();
      await page.waitForTimeout(100);
      await check(
        page
          .getByRole("heading", { name: "Operations overview", exact: true })
          .isVisible(),
        "Root URL must show Overview after Back",
      );
    },
  );
  await scenario(
    "fresh: a JSON-null 401 still expires the session",
    async ({ page, override, respond, check }) => {
      override(async ({ route, url }) => {
        if (url.pathname === "/v1/overview") {
          await respond(route, null, 401);
          return true;
        }
        return false;
      });
      await page.getByRole("button", { name: "Refresh workspace" }).click();
      await page.waitForTimeout(200);
      await check(
        page
          .getByRole("heading", { name: "Connect to OrderFlow", exact: true })
          .isVisible(),
        "401 must clear the workspace even when its JSON body is null",
      );
    },
  );
  await scenario(
    "fresh: JSON-null service errors retain an actionable status and request ID",
    async ({ page, override, check }) => {
      override(async ({ route, url }) => {
        if (url.pathname === "/v1/overview") {
          await route.fulfill({
            status: 503,
            contentType: "application/json",
            headers: { "X-Request-ID": "fresh-null-503" },
            body: "null",
          });
          return true;
        }
        return false;
      });
      await page.getByRole("button", { name: "Refresh workspace" }).click();
      await page.getByRole("alert").waitFor();
      await check(
        page
          .getByRole("alert")
          .getByText("fresh-null-503", { exact: true })
          .isVisible(),
        "Service errors must preserve request ID when body is null",
      );
    },
  );
  await scenario(
    "fresh: an old retry401 cannot expire a reconnected session using the same token",
    async ({ page, go, override, respond, check }) => {
      let release, observed;
      const pending = new Promise((r) => (observed = r));
      override(async ({ route, url, request }) => {
        if (
          url.pathname === "/v1/events/retry-event/retry" &&
          request.method() === "POST"
        ) {
          observed();
          await new Promise((r) => (release = r));
          await respond(
            route,
            { code: "old_expired_token", message: "OLD-RETRY-401" },
            401,
          );
          return true;
        }
        return false;
      });
      await go("Overview");
      await go("Event inbox");
      await page
        .getByRole("button", { name: "order-retry-event", exact: true })
        .click();
      await page
        .getByLabel("Operational reason", { exact: false })
        .fill("Reviewed dependency repair");
      await page
        .getByRole("button", { name: "Request retry", exact: true })
        .click();
      await pending;
      await page.goBack();
      await page
        .getByRole("heading", { name: "Operations overview", exact: true })
        .waitFor();
      await page.getByRole("button", { name: "Disconnect session" }).click();
      await page
        .getByRole("button", { name: "Connect local workspace", exact: true })
        .click();
      await page
        .getByRole("heading", { name: "Operations overview", exact: true })
        .waitFor();
      release();
      await page.waitForTimeout(200);
      await check(
        page
          .getByRole("heading", { name: "Operations overview", exact: true })
          .isVisible(),
        "Old session401 must not clear a new session with the same bearer token",
      );
    },
  );
  await scenario(
    "fresh: all views fit320/375/768/1440 and200percent text",
    async ({ page, go, check }) => {
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
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
          await page.waitForFunction(() => !document.querySelector(".loading"));
          await check(
            page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth + 1,
            ),
            `No viewport overflow ${width}/${name}`,
          );
        }
      }
      await page.setViewportSize({ width: 1280, height: 900 });
      await page.evaluate(
        () => (document.documentElement.style.fontSize = "200%"),
      );
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
        await page.waitForFunction(() => !document.querySelector(".loading"));
        await check(
          page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth + 1,
          ),
          `No viewport overflow 200percent/${name}`,
        );
      }
    },
  );
} finally {
  await finishHarness(5);
}
