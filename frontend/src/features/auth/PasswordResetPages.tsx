// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Password reset by email (SECURITY.md A11). The request always answers the
 * same way, whether or not the account exists. The emailed link carries the
 * token in the fragment (`/reset-password#token=...`), which never reaches a
 * server log; the page reads it, removes it from the address bar and sends it
 * in the request body.
 */
import { Link } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { confirmPasswordReset, requestPasswordReset } from "./api";
import { AuthLayout, useGoTo, useLocationParts } from "./AuthLayout";
import { authErrorMessage, fieldErrorsOf, passwordProblem } from "./errors";
import { FORGOT_PATH, LOGIN_PATH } from "./guard";
import { linkSecret } from "./fragment";

export function ForgotPasswordPage() {
  const [identifier, setIdentifier] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (identifier.trim() === "") {
      setError("Enter your username or email address.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await requestPasswordReset(identifier.trim());
      setSent(true);
    } catch (caught) {
      setError(authErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  if (sent) {
    return (
      <AuthLayout title="Check your email">
        <p role="status">
          If an account matches, a reset link is on its way. It works once and expires in 15 minutes.
        </p>
        <Link to={LOGIN_PATH}>Back to sign in</Link>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Reset your password" intro={<p>We will email a reset link to the address on your account.</p>}>
      <form
        className="auth-stack"
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        <Field label="Username or email" error={error} required>
          <Input
            name="identifier"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            value={identifier}
            onChange={(event) => {
              setIdentifier(event.target.value);
            }}
          />
        </Field>
        <Button type="submit" variant="primary" loading={busy}>
          Send reset link
        </Button>
        <Link to={LOGIN_PATH}>Back to sign in</Link>
      </form>
    </AuthLayout>
  );
}

export function ResetPasswordPage() {
  const goTo = useGoTo();
  const { pathname, search, hash } = useLocationParts();
  // Read the token once; it is then removed from the address bar.
  const [resetKey] = useState(() => linkSecret(hash, search, ["token"]));
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (hash !== "" || search !== "") {
      void goTo(pathname, { replace: true });
    }
  }, [goTo, hash, pathname, search]);

  if (resetKey === null) {
    return (
      <AuthLayout title="Reset your password">
        <p role="alert">This reset link is incomplete. Open the link from the email again, or ask for a new one.</p>
        <Link to={FORGOT_PATH}>Ask for a new link</Link>
      </AuthLayout>
    );
  }

  if (done) {
    return (
      <AuthLayout title="Password changed">
        <p role="status">Your password was changed and every session was signed out.</p>
        <Link to={LOGIN_PATH} className="btn btn--primary btn--md">
          Sign in
        </Link>
      </AuthLayout>
    );
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const problem = passwordProblem(password, confirm);
    if (problem !== null) {
      setFieldErrors(password !== confirm && password.length >= 12 ? { confirm: problem } : { new_password: problem });
      return;
    }
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      await confirmPasswordReset(resetKey, password);
      setPassword("");
      setConfirm("");
      setDone(true);
    } catch (caught) {
      setError(authErrorMessage(caught));
      setFieldErrors(fieldErrorsOf(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout title="Choose a new password">
      <form
        className="auth-stack"
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        <Field label="New password" hint="12 to 128 characters." error={fieldErrors["new_password"]} required>
          <Input
            name="new-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
        </Field>
        <Field label="Repeat the new password" error={fieldErrors["confirm"]} required>
          <Input
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => {
              setConfirm(event.target.value);
            }}
          />
        </Field>
        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" loading={busy}>
          Change password
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            void goTo(LOGIN_PATH);
          }}
        >
          Cancel
        </Button>
      </form>
    </AuthLayout>
  );
}
