// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * First-run setup (SECURITY.md A10): with no user yet, `perfin api` prints a
 * one-time link `<public url>/setup#t=<token>`. This page reads the token
 * from the fragment (or the query, if a client rewrote the link), removes it
 * from the address bar and creates the first instance administrator and
 * household with `POST /setup`, which also signs them in. Once any user
 * exists, `GET /setup/status` says so and the page points to sign-in.
 */
import "../auth/auth.css";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { api, unwrap } from "../../lib/api/client";
import { getSetupRequired } from "../auth/api";
import { AuthLayout, useGoTo, useLocationParts } from "../auth/AuthLayout";
import { authErrorMessage, fieldErrorsOf, passwordProblem } from "../auth/errors";
import { linkSecret } from "../auth/fragment";
import { LOGIN_PATH } from "../auth/guard";
import { sessionChanged } from "../auth/session";

const USERNAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

interface FormState {
  username: string;
  displayName: string;
  password: string;
  confirm: string;
  householdName: string;
  baseCurrency: string;
}

const EMPTY: FormState = {
  username: "",
  displayName: "",
  password: "",
  confirm: "",
  householdName: "",
  baseCurrency: "USD",
};

function validate(form: FormState): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!USERNAME_RE.test(form.username.trim().toLowerCase())) {
    errors["username"] = "Use 1-64 lower-case letters, digits, '.', '_' or '-', starting with a letter or digit.";
  }
  if (form.displayName.trim() === "") {
    errors["display_name"] = "Enter your name.";
  }
  const passwordIssue = passwordProblem(form.password, form.confirm);
  if (passwordIssue !== null) {
    errors[form.password !== form.confirm && form.password.length >= 12 ? "confirm" : "password"] = passwordIssue;
  }
  if (form.householdName.trim() === "") {
    errors["household_name"] = "Name the household, for example your family name.";
  }
  if (!/^[A-Z]{3}$/.test(form.baseCurrency.trim().toUpperCase())) {
    errors["base_currency"] = "Use a three-letter currency code such as USD or EUR.";
  }
  return errors;
}

export function SetupPage() {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const { pathname, search, hash } = useLocationParts();
  const [setupKey] = useState(() => linkSecret(hash, search, ["t", "token"]));
  const [form, setForm] = useState<FormState>(EMPTY);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const status = useQuery({ queryKey: ["setup", "status"], queryFn: getSetupRequired, retry: false });

  // The token is a one-time secret: keep it out of the address bar and history.
  useEffect(() => {
    if (hash !== "" || search !== "") {
      void goTo(pathname, { replace: true });
    }
  }, [goTo, hash, pathname, search]);

  if (status.isPending) {
    return (
      <AuthLayout title="Set up PerFin">
        <Spinner label="Checking setup status" />
      </AuthLayout>
    );
  }

  if (status.data === false) {
    return (
      <AuthLayout title="PerFin is set up">
        <p role="status">This server already has an administrator. Sign in instead.</p>
        <Link to={LOGIN_PATH} className="btn btn--primary btn--md">
          Sign in
        </Link>
      </AuthLayout>
    );
  }

  if (setupKey === null) {
    return (
      <AuthLayout title="Set up PerFin">
        <p role="alert">
          Open the setup link printed in the server log. If it expired, run <code>perfin bootstrap-url</code> on the
          server for a new one.
        </p>
      </AuthLayout>
    );
  }

  const update =
    (key: keyof FormState) =>
    (event: { target: { value: string } }): void => {
      const { value } = event.target;
      setForm((previous) => ({ ...previous, [key]: value }));
    };

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const found = validate(form);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await unwrap(
        api.POST("/api/v1/setup", {
          body: {
            token: setupKey,
            username: form.username.trim().toLowerCase(),
            display_name: form.displayName.trim(),
            password: form.password,
            household_name: form.householdName.trim(),
            base_currency: form.baseCurrency.trim().toUpperCase(),
          },
        }),
      );
      setForm(EMPTY);
      await sessionChanged(queryClient, result.csrf_token);
      await goTo("/", { replace: true });
    } catch (caught) {
      setError(authErrorMessage(caught));
      setErrors(fieldErrorsOf(caught));
      setBusy(false);
    }
  };

  return (
    <AuthLayout
      title="Set up PerFin"
      intro={<p>Create the administrator account and your first household. You can add people later.</p>}
    >
      <form
        className="auth-stack"
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        <Field label="Username" hint="Lower-case letters, digits, '.', '_' or '-'." error={errors["username"]} required>
          <Input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} value={form.username} onChange={update("username")} />
        </Field>
        <Field label="Your name" error={errors["display_name"]} required>
          <Input name="display-name" autoComplete="name" value={form.displayName} onChange={update("displayName")} />
        </Field>
        <Field label="Password" hint="12 to 128 characters." error={errors["password"]} required>
          <Input name="password" type="password" autoComplete="new-password" value={form.password} onChange={update("password")} />
        </Field>
        <Field label="Repeat the password" error={errors["confirm"]} required>
          <Input name="confirm-password" type="password" autoComplete="new-password" value={form.confirm} onChange={update("confirm")} />
        </Field>
        <Field label="Household name" error={errors["household_name"]} required>
          <Input name="household-name" value={form.householdName} onChange={update("householdName")} />
        </Field>
        <Field label="Base currency" hint="ISO 4217 code; amounts are reported in it." error={errors["base_currency"]} required>
          <Input name="base-currency" maxLength={3} autoCapitalize="characters" value={form.baseCurrency} onChange={update("baseCurrency")} />
        </Field>
        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" loading={busy}>
          Create administrator
        </Button>
      </form>
    </AuthLayout>
  );
}
