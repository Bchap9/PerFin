// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Sign in (docs/AUTH.md): the local username/password form when the server
 * offers it (`GET /auth/login/options`), a "Sign in with Authentik" button
 * when OIDC is configured (`GET /auth/oidc/status`), and a notice while
 * first-run setup is pending. A successful password login either finishes
 * the login or continues at the second-factor step.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { getLoginOptions, getOidcEnabled, getSetupRequired, login, oidcStartHref } from "./api";
import { AuthLayout, useGoTo, useLocationParts } from "./AuthLayout";
import { authErrorMessage, fieldErrorsOf } from "./errors";
import { FORGOT_PATH, MFA_PATH } from "./guard";
import { returnToFromSearch, withReturnTo } from "./returnTo";
import { sessionChanged } from "./session";

const OIDC_ERRORS = new Map<string, string>(Object.entries({
  "rate-limited": "Too many sign-in attempts. Wait a few minutes and try again.",
  "state-invalid": "The sign-in took too long or was started in another tab. Try again.",
  "idp-error": "The identity provider refused the sign-in.",
  "idp-unavailable": "The identity provider could not be reached. Try again later.",
  "token-invalid": "The identity provider's answer could not be verified.",
  "not-in-group": "Your account is not allowed to use PerFin. Ask your administrator.",
  "no-account": "There is no PerFin account for you yet. Ask your administrator.",
  "account-exists": "A PerFin account with your email already exists. Sign in with its password to link it.",
  "account-disabled": "This account is disabled.",
  "login-failed": "Single sign-on failed. Try again.",
}));

export const loginOptionsKey = ["auth", "login-options"] as const;

function oidcErrorMessage(search: string): string | null {
  const code = new URLSearchParams(search).get("oidc_error");
  if (code === null) {
    return null;
  }
  return OIDC_ERRORS.get(code) ?? OIDC_ERRORS.get("login-failed") ?? null;
}

/** The local password form; also used on the "link your account" page. */
export function LocalLoginForm({ returnTo, showForgot }: { returnTo: string; showForgot: boolean }) {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const passwordRef = useRef<HTMLInputElement>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const missing: Record<string, string> = {};
    if (username.trim() === "") {
      missing["username"] = "Enter your username.";
    }
    if (password.length === 0) {
      missing["password"] = "Enter your password.";
    }
    setFieldErrors(missing);
    if (Object.keys(missing).length > 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await login({ username: username.trim(), password, remember_device: remember });
      setPassword("");
      await sessionChanged(queryClient, result.csrf_token);
      await goTo(result.status === "mfa_required" ? withReturnTo(MFA_PATH, returnTo) : returnTo, { replace: true });
    } catch (caught) {
      setPassword("");
      setError(authErrorMessage(caught));
      setFieldErrors(fieldErrorsOf(caught));
      setBusy(false);
      passwordRef.current?.focus();
    }
  };

  return (
    <form
      className="auth-stack"
      noValidate
      aria-label="Sign in with your password"
      onSubmit={(event) => {
        void onSubmit(event);
      }}
    >
      <Field label="Username" error={fieldErrors["username"]} required>
        <Input
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          value={username}
          onChange={(event) => {
            setUsername(event.target.value);
          }}
        />
      </Field>
      <Field label="Password" error={fieldErrors["password"]} required>
        <Input
          ref={passwordRef}
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
        />
      </Field>
      <Checkbox
        label="Keep me signed in on this device"
        checked={remember}
        onChange={(event) => {
          setRemember(event.target.checked);
        }}
      />
      {error === null ? null : (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      <Button type="submit" variant="primary" loading={busy}>
        Sign in
      </Button>
      {showForgot ? (
        <p className="auth-muted">
          <Link to={FORGOT_PATH} className="link-action">
            Forgot your password?
          </Link>
        </p>
      ) : null}
    </form>
  );
}

export function LoginPage() {
  const { search } = useLocationParts();
  const returnTo = returnToFromSearch(search);
  const oidcError = oidcErrorMessage(search);
  const options = useQuery({ queryKey: loginOptionsKey, queryFn: getLoginOptions, retry: false, staleTime: 60_000 });
  const oidc = useQuery({ queryKey: [...loginOptionsKey, "oidc"], queryFn: getOidcEnabled, retry: false, staleTime: 60_000 });
  const setup = useQuery({ queryKey: [...loginOptionsKey, "setup"], queryFn: getSetupRequired, retry: false, staleTime: 60_000 });

  if (options.isPending) {
    return (
      <AuthLayout title="Sign in">
        <Spinner label="Loading sign-in options" />
      </AuthLayout>
    );
  }
  // If the options cannot be read, offer the password form: the server decides.
  const localLogin = options.data?.local_login ?? true;
  const passwordReset = options.data?.password_reset ?? false;
  const oidcEnabled = oidc.data === true;

  return (
    <AuthLayout title="Sign in">
      {setup.data === true ? (
        <p className="auth-notice" role="status">
          PerFin is not set up yet. Open the setup link from the server log (or run <code>perfin bootstrap-url</code>) to
          create the first account.
        </p>
      ) : null}
      {oidcError === null ? null : (
        <p className="auth-error" role="alert">
          {oidcError}
        </p>
      )}
      {oidcEnabled ? (
        <a className="btn btn--primary btn--md auth-sso" href={oidcStartHref(returnTo)}>
          <span className="btn__label">Sign in with Authentik</span>
        </a>
      ) : null}
      {oidcEnabled && localLogin ? <p className="auth-divider">or use your PerFin password</p> : null}
      {localLogin ? <LocalLoginForm returnTo={returnTo} showForgot={passwordReset} /> : null}
      {!localLogin && !oidcEnabled ? (
        <p className="auth-error" role="alert">
          No sign-in method is available from this network. Ask your administrator.
        </p>
      ) : null}
    </AuthLayout>
  );
}
