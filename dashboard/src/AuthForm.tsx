import { useEffect, useRef, useState } from "react";
import {
  neonAccountAction,
  passwordResetIssue,
  passwordResetPending,
} from "./auth";
import type { Config } from "./types";
type Mode =
  | "signin"
  | "signup"
  | "verify"
  | "reset-request"
  | "reset-code"
  | "reset-confirm";
export function AuthForm({
  config,
  busy,
  signingOut,
  signIn,
  signOut,
  notice,
  error,
}: {
  config: Config;
  busy: boolean;
  signingOut: boolean;
  signIn: (credentials: { email: string; password: string }) => void;
  signOut: () => void;
  notice: (message: string) => void;
  error: (error: unknown) => void;
}) {
  const [mode, setMode] = useState<Mode>(() =>
      passwordResetPending()
        ? "reset-confirm"
        : passwordResetIssue()
          ? "reset-request"
          : "signin",
    ),
    [email, setEmail] = useState(""),
    [password, setPassword] = useState(""),
    [name, setName] = useState(""),
    [code, setCode] = useState(""),
    [working, setWorking] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const disabled = busy || signingOut || working;
  const action = async (
    kind:
      | "signup"
      | "verify"
      | "resend"
      | "reset-request"
      | "reset-code"
      | "reset-confirm",
  ) => {
    const values = { email, password, name, code };
    setPassword("");
    setCode("");
    setWorking(true);
    try {
      const unverified = await neonAccountAction(config, kind, values);
      if (!alive.current) return;
      if (kind === "signup") {
        setMode(unverified ? "verify" : "signin");
        notice(
          unverified
            ? "If a new account needs verification, check your email for a code. Already registered? Return to sign in or reset your password. Workspace access requires owner approval."
            : "If your account is ready, sign in. Otherwise check your email for verification instructions. Workspace access requires owner approval.",
        );
      } else if (kind === "verify") {
        setMode("signin");
        notice(
          "Email verified. Sign in after the workspace owner grants access.",
        );
      } else if (kind === "resend")
        notice(
          "If verification is available for this account, check your email for a new code.",
        );
      else if (kind === "reset-request") {
        setMode("reset-code");
        notice(
          "If the account exists, a password reset code has been sent. Check your email.",
        );
      } else {
        setMode("signin");
        notice("Password reset. Sign in with your new password.");
      }
    } catch (cause) {
      if (alive.current) error(cause);
    } finally {
      if (alive.current) setWorking(false);
    }
  };
  const switchMode = (next: Mode) => {
    setPassword("");
    setCode("");
    setMode(next);
  };
  return (
    <form
      className="connect-options"
      aria-label="Workspace account"
      aria-busy={disabled}
      onSubmit={(event) => {
        event.preventDefault();
        if (disabled) return;
        if (mode === "signin") {
          const credentials = { email, password };
          setPassword("");
          signIn(credentials);
        } else void action(mode);
      }}
    >
      {passwordResetIssue() && mode === "reset-request" && (
        <p role="alert">
          This reset link is invalid or expired. Request a new reset link.
        </p>
      )}
      {mode === "signup" && (
        <>
          <label htmlFor="login-name">Name</label>
          <input
            id="login-name"
            autoComplete="name"
            value={name}
            required
            disabled={disabled}
            onChange={(e) => setName(e.target.value)}
          />
        </>
      )}
      {mode !== "reset-confirm" && (
        <>
          <label htmlFor="login-email">Email</label>
          <input
            id="login-email"
            type="email"
            autoComplete="username"
            value={email}
            required
            disabled={disabled}
            onChange={(e) => setEmail(e.target.value)}
          />
        </>
      )}
      {["signin", "signup", "reset-code", "reset-confirm"].includes(mode) && (
        <>
          <label htmlFor="login-password">
            {mode.startsWith("reset-") ? "New password" : "Password"}
          </label>
          <input
            id="login-password"
            type="password"
            autoComplete={
              mode === "signin" ? "current-password" : "new-password"
            }
            minLength={mode === "signin" ? undefined : 8}
            value={password}
            required
            disabled={disabled}
            onChange={(e) => setPassword(e.target.value)}
          />
        </>
      )}
      {mode === "verify" && (
        <>
          <label htmlFor="verification-code">Verification code</label>
          <input
            id="verification-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            required
            disabled={disabled}
            onChange={(e) => setCode(e.target.value)}
          />
          <small>
            Already registered? Return to sign in or reset your password. Enter a verification code here only if one was sent to your email.
          </small>
        </>
      )}
      {mode === "reset-code" && (
        <>
          <label htmlFor="reset-code">Password reset code</label>
          <input
            id="reset-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            required
            disabled={disabled}
            onChange={(e) => setCode(e.target.value)}
          />
        </>
      )}
      <button
        className="button primary"
        disabled={disabled || !config.configured}
      >
        {signingOut
          ? "Signing out…"
          : disabled
            ? "Working…"
            : {
                signin: "Sign in",
                signup: "Create account",
                verify: "Verify email",
                "reset-request": "Send reset code",
                "reset-code": "Set new password",
                "reset-confirm": "Set new password",
              }[mode]}
      </button>
      <small>
        Use your verified workspace account. Access requires a membership
        granted by the workspace owner.
      </small>
      {mode === "signin" ? (
        <>
          <button
            type="button"
            className="button secondary"
            disabled={disabled}
            onClick={() => switchMode("signup")}
          >
            Create an account
          </button>
          <button
            type="button"
            className="text-button"
            disabled={disabled}
            onClick={() => switchMode("reset-request")}
          >
            Forgot password?
          </button>
          <button
            type="button"
            className="text-button"
            disabled={disabled}
            onClick={() => switchMode("verify")}
          >
            Verify your email
          </button>
        </>
      ) : (
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => switchMode("signin")}
        >
          Back to sign in
        </button>
      )}
      {mode === "verify" && (
        <button
          type="button"
          className="text-button"
          disabled={disabled || !email}
          onClick={() => void action("resend")}
        >
          Resend verification code
        </button>
      )}
      {mode === "verify" && (
        <button type="button" className="text-button" disabled={disabled} onClick={() => switchMode("reset-request")}>Forgot password?</button>
      )}
      {mode === "reset-code" && (
        <button
          type="button"
          className="text-button"
          disabled={disabled}
          onClick={() => switchMode("reset-request")}
        >
          Request a new reset code
        </button>
      )}
      <button
        type="button"
        className="button secondary"
        disabled={disabled}
        onClick={signOut}
      >
        Sign out of identity provider
      </button>
    </form>
  );
}
