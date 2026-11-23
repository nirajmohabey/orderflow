import { createAuthClient } from "better-auth/client";
import { emailOTPClient, jwtClient } from "better-auth/client/plugins";
import type { Config } from "./types";

const createClient = () =>
  createAuthClient({
    baseURL: new URL("/auth", location.origin).href,
    plugins: [jwtClient(), emailOTPClient()],
    fetchOptions: { timeout: 15000, retry: 0 },
  });
let client: ReturnType<typeof createClient> | null = null;
let resetToken: string | null = null;
let resetIssue = false;
const callback = new URL(location.href);
if (callback.searchParams.has("reset_password")) {
  const value = callback.searchParams.get("token");
  resetToken = value && value.length <= 4096 ? value : null;
  resetIssue = !resetToken;
  callback.searchParams.delete("token");
  callback.searchParams.delete("reset_password");
  callback.searchParams.delete("error");
  history.replaceState({}, "", callback);
}
export const passwordResetPending = () => !!resetToken;
export const passwordResetIssue = () => resetIssue;
function auth(config: Config) {
  if (
    config.mode !== "neon" ||
    !config.configured ||
    config.auth_base_url !== "/auth"
  )
    throw new Error("Workspace sign-in is not configured.");
  return (client ??= createClient());
}
export async function neonToken(
  config: Config,
  credentials?: { email: string; password: string },
) {
  const provider = auth(config);
  if (credentials) {
    const result = await provider.signIn.email(credentials);
    if (result.error)
      throw new Error(
        result.error.status === 403
          ? "Verify your email before signing in. Contact the workspace owner if you need access."
          : "Sign-in failed. Check your email and password, then try again.",
      );
  }
  const session = await provider.getSession();
  if (session.error)
    throw new Error("Sign-in is temporarily unavailable. Try again.");
  if (!session.data?.session) return null;
  // Explicit JWT endpoint: Better Auth's opaque cookie-session token is never
  // passed to the application API, even if an upstream JWT header is absent.
  const result = await provider.token();
  const token = result.data?.token;
  if (
    result.error ||
    typeof token !== "string" ||
    token.split(".").length !== 3
  )
    throw new Error("Your sign-in could not be verified. Sign in again.");
  return token;
}
export async function neonSignOut(config: Config) {
  const result = await auth(config).signOut();
  if (result.error)
    throw new Error(
      "The workspace disconnected, but provider sign-out failed. Try signing out again.",
    );
}
export async function neonAccountAction(
  config: Config,
  action:
    | "signup"
    | "verify"
    | "resend"
    | "reset-request"
    | "reset-code"
    | "reset-confirm",
  values: { email: string; password?: string; name?: string; code?: string },
) {
  const provider = auth(config);
  const result =
    action === "signup"
      ? await provider.signUp.email({
          email: values.email,
          password: values.password!,
          name: values.name!,
          callbackURL: location.origin + "/",
        })
      : action === "verify"
        ? await provider.emailOtp.verifyEmail({
            email: values.email,
            otp: values.code!,
          })
        : action === "resend"
          ? await provider.emailOtp.sendVerificationOtp({
              email: values.email,
              type: "email-verification",
            })
          : action === "reset-request"
            ? await provider.emailOtp.requestPasswordReset({
                email: values.email,
              })
            : action === "reset-code"
              ? await provider.emailOtp.resetPassword({
                  email: values.email,
                  otp: values.code!,
                  password: values.password!,
                })
              : await provider.resetPassword({
                  newPassword: values.password!,
                  token: resetToken ?? "",
                });
  if (result.error)
    throw new Error(
      action === "reset-confirm"
        ? "The password could not be reset. Check the new password or request a fresh reset link."
        : "The request could not be completed. Check your details and try again.",
    );
  if (action === "reset-confirm") {
    resetToken = null;
    resetIssue = false;
  }
  return (
    action === "signup" &&
    result.data &&
    "user" in result.data &&
    !result.data.user.emailVerified
  );
}
