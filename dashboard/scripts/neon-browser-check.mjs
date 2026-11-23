/** Real Better Auth client, fictional provider/API. No external credentials. */
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { config, installProvider, password } from "./neon-provider-fixture.mjs";
assert.ok(process.argv[2], "Provide explicit scratch output directory");
process.env.ORDERFLOW_BROWSER_OUT = path.resolve(process.argv[2]);
const { scenario, finishHarness, OUT, overview } =
  await import("./browser-check.mjs");
const storage = [];
async function run(name, options, test) {
  let provider;
  await scenario(
    name,
    async (context) => {
      if (!options.signedIn && !options.resetCallback)
        await context.page.locator("#login-email").waitFor();
      await test({ ...context, provider });
      storage.push({ name, ...(await provider.assertNoPersistence()) });
    },
    {
      fixture: false,
      authConfig: config,
      urlSuffix: options.urlSuffix,
      prepare: async ({ page, override, respond }) => {
        override(async ({ route, url }) => {
          if (url.pathname === "/v1/session") {
            await respond(
              route,
              options.membershipDenied
                ? { code: "membership_required" }
                : {
                    principal: {
                      tenant_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                      subject: "synthetic-user",
                      role: "viewer",
                    },
                    tenant: {
                      tenant_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                      name: "Alpha Retail",
                      currency: "USD",
                    },
                  },
              options.membershipDenied ? 403 : 200,
            );
            return true;
          }
          if (url.pathname === "/v1/overview") {
            await respond(route, overview("alpha"));
            return true;
          }
          return false;
        });
        provider = await installProvider(page, options);
      },
    },
  );
}
const submit = async (page) => {
  await page.locator("#login-email").fill("browser-fixture@example.invalid");
  await page.locator("#login-password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
};
const workspace = (page) =>
  page.getByRole("heading", { name: "Operations overview", exact: true });
try {
  await run(
    "Neon sign-in clears submitted password and signs out safely",
    { holdSignIn: true },
    async ({ page, provider }) => {
      await submit(page);
      await provider.signInObserved;
      assert.equal(await page.locator("#login-password").inputValue(), "");
      assert.equal(await page.locator("#login-password").isDisabled(), true);
      provider.releaseSignIn();
      await workspace(page).waitFor();
      assert.equal(provider.sentTokens.length, 1);
      await page.getByRole("button", { name: "Disconnect session" }).click();
      await page.locator("#login-email").waitFor();
      await page.waitForFunction(
        () => !document.querySelector("button[disabled]"),
      );
      assert.ok(provider.calls.some((call) => call.path === "/auth/sign-out"));
    },
  );
  await run(
    "Unenrolled identity gets access-request notice and no tenant reads",
    { membershipDenied: true },
    async ({ page, requests }) => {
      await submit(page);
      await page
        .getByRole("alert")
        .filter({ hasText: "no active workspace membership" })
        .waitFor();
      assert.ok(!requests.some((request) => request.path === "/v1/overview"));
      assert.equal(
        await page.getByRole("button", { name: "Disconnect session" }).count(),
        0,
      );
    },
  );
  await run(
    "Invalid provider password clears password and allows retry",
    { passwordFailure: true },
    async ({ page, provider }) => {
      await submit(page);
      await page
        .getByRole("alert")
        .filter({ hasText: "Check your email and password" })
        .waitFor();
      assert.equal(await page.locator("#login-password").inputValue(), "");
      assert.equal(provider.sentTokens.length, 0);
    },
  );
  await run(
    "Opaque provider token is never sent to app membership endpoint",
    { opaqueToken: true },
    async ({ page, requests }) => {
      await submit(page);
      await page
        .getByRole("alert")
        .filter({ hasText: "could not be verified" })
        .waitFor();
      assert.ok(!requests.some((request) => request.path === "/v1/session"));
    },
  );
  await run(
    "Provider sign-out failure preserves local disconnect",
    { signOutFailure: true },
    async ({ page }) => {
      await submit(page);
      await workspace(page).waitFor();
      await page.getByRole("button", { name: "Disconnect session" }).click();
      await page
        .getByRole("alert")
        .filter({ hasText: "provider sign-out failed" })
        .waitFor();
      assert.equal(await workspace(page).count(), 0);
    },
  );
  await run(
    "Reload resumes cookie but obtains a new API JWT",
    { signedIn: true },
    async ({ page, provider }) => {
      await workspace(page).waitFor();
      await page.reload();
      await workspace(page).waitFor();
      assert.equal(provider.sentTokens.length, 2);
      assert.notEqual(provider.sentTokens[0], provider.sentTokens[1]);
    },
  );
  await run(
    "Expired JWT clears the verified workspace",
    { expiresIn: 2 },
    async ({ page }) => {
      await submit(page);
      await workspace(page).waitFor();
      await page
        .getByRole("alert")
        .filter({ hasText: "session expired" })
        .waitFor();
      assert.equal(await workspace(page).count(), 0);
    },
  );
  await run(
    "Signup and verification require separate owner-granted membership",
    { signupUnverified: true },
    async ({ page, provider, requests }) => {
      await page
        .getByRole("button", { name: "Create an account", exact: true })
        .click();
      await page.locator("#login-name").fill("Fictional test user");
      await page
        .locator("#login-email")
        .fill("browser-fixture@example.invalid");
      await page.locator("#login-password").fill(password);
      await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
      await page.locator("#verification-code").waitFor();
      await page
        .getByRole("button", { name: "Resend verification email" })
        .click();
      await page
        .getByRole("status")
        .filter({ hasText: "new code or link" })
        .waitFor();
      await page.locator("#verification-code").fill("123456");
      await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
      await page
        .getByRole("status")
        .filter({ hasText: "Email verified" })
        .waitFor();
      assert.ok(
        provider.calls.some(
          (call) => call.path === "/auth/email-otp/verify-email",
        ),
      );
      assert.ok(!requests.some((request) => request.path === "/v1/session"));
    },
  );
  await run(
    "Forgot-password request has a same-origin callback",
    {},
    async ({ page, provider }) => {
      await page.getByRole("button", { name: "Forgot password?" }).click();
      await page
        .locator("#login-email")
        .fill("browser-fixture@example.invalid");
      await page
        .getByRole("button", { name: "Send reset code", exact: true })
        .click();
      await page
        .getByRole("status")
        .filter({ hasText: "If the account exists" })
        .waitFor();
      assert.ok(
        provider.calls.some(
          (call) => call.path === "/auth/email-otp/request-password-reset",
        ),
      );
      await page.getByLabel("New password", { exact: true }).fill(password);
      await page.locator("#reset-code").fill("123456");
      await page
        .getByRole("button", { name: "Set new password", exact: true })
        .click();
      await page
        .getByRole("status")
        .filter({ hasText: "Password reset" })
        .waitFor();
      assert.ok(!page.url().includes("token="));
    },
  );
  await run(
    "Reset callback removes URL token before API and completes reset",
    {
      resetCallback: true,
      signedIn: true,
      resetToken: "synthetic-reset-credential",
      urlSuffix: "/?reset_password=true&token=synthetic-reset-credential",
    },
    async ({ page, requests }) => {
      await page.getByLabel("New password", { exact: true }).waitFor();
      assert.ok(!page.url().includes("synthetic-reset-credential"));
      assert.ok(!requests.some((request) => request.path === "/v1/session"));
      await page.getByLabel("New password", { exact: true }).fill(password);
      await page
        .getByRole("button", { name: "Set new password", exact: true })
        .click();
      await page
        .getByRole("status")
        .filter({ hasText: "Password reset" })
        .waitFor();
      assert.equal(await page.locator("#login-password").inputValue(), "");
    },
  );
  await run(
    "Expired reset callback offers a new link and clears URL error",
    {
      resetCallback: true,
      urlSuffix: "/?reset_password=true&error=INVALID_TOKEN",
    },
    async ({ page }) => {
      await page
        .getByRole("alert")
        .filter({ hasText: "reset link is invalid or expired" })
        .waitFor();
      assert.ok(!page.url().includes("INVALID_TOKEN"));
      assert.equal(
        await page
          .getByRole("button", { name: "Send reset code", exact: true })
          .isVisible(),
        true,
      );
    },
  );
  await run(
    "Invalid reset code clears secrets and permits another request",
    { invalidOtp: true },
    async ({ page }) => {
      await page.getByRole("button", { name: "Forgot password?" }).click();
      await page
        .locator("#login-email")
        .fill("browser-fixture@example.invalid");
      await page
        .getByRole("button", { name: "Send reset code", exact: true })
        .click();
      await page.locator("#reset-code").waitFor();
      await page.getByLabel("New password", { exact: true }).fill(password);
      await page.locator("#reset-code").fill("123456");
      await page
        .getByRole("button", { name: "Set new password", exact: true })
        .click();
      await page
        .getByRole("alert")
        .filter({ hasText: "could not be completed" })
        .waitFor();
      assert.equal(await page.locator("#reset-code").inputValue(), "");
      assert.equal(await page.locator("#login-password").inputValue(), "");
      await page
        .getByRole("button", { name: "Request a new reset code", exact: true })
        .click();
      assert.equal(
        await page
          .getByRole("button", { name: "Send reset code", exact: true })
          .isEnabled(),
        true,
      );
    },
  );
} finally {
  await finishHarness(12);
  const report = JSON.parse(
    await fs.readFile(path.join(OUT, "browser-results.json"), "utf8"),
  );
  await fs.writeFile(
    path.join(OUT, "browser-results.json"),
    JSON.stringify(
      {
        ...report,
        storage_checks: storage,
        external_identity_provider_tested: false,
      },
      null,
      2,
    ) + "\n",
  );
}
