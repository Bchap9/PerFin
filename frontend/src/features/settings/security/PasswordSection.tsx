// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Change password (`POST /auth/password`): needs the current password and,
 * when the account has a second factor, a recent step-up. The server rotates
 * this session and signs out every other one.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { useToast } from "../../../components/Toast";
import { isApiError } from "../../../lib/api/client";
import { changePassword } from "../../auth/api";
import { authErrorMessage, fieldErrorsOf, passwordProblem } from "../../auth/errors";
import { sessionRotated } from "../../auth/session";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";

const EMPTY = { current: "", next: "", confirm: "" };

export function PasswordSection() {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const [form, setForm] = useState(EMPTY);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (form.current === "") {
      found["current_password"] = "Enter your current password.";
    }
    const problem = passwordProblem(form.next, form.confirm);
    if (problem !== null) {
      found[form.next !== form.confirm && form.next.length >= 12 ? "confirm" : "new_password"] = problem;
    }
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const changed = await runWithStepUp(() =>
        changePassword({ current_password: form.current, new_password: form.next }),
      );
      sessionRotated(queryClient, changed.csrf_token);
      setForm(EMPTY);
      notify({
        tone: "success",
        title: "Password changed",
        description:
          changed.sessions_revoked > 0
            ? `${String(changed.sessions_revoked)} other sessions were signed out.`
            : "No other sessions were signed in.",
      });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        if (isApiError(caught) && caught.slug === "invalid-credentials") {
          setErrors({ current_password: "The current password is wrong." });
        } else {
          setError(authErrorMessage(caught));
          setErrors(fieldErrorsOf(caught));
        }
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Password">
      <form
        className="auth-stack"
        noValidate
        onSubmit={(event) => {
          void onSubmit(event);
        }}
      >
        <Field label="Current password" error={errors["current_password"]} required>
          <Input
            name="current-password"
            type="password"
            autoComplete="current-password"
            value={form.current}
            onChange={(event) => {
              setForm({ ...form, current: event.target.value });
            }}
          />
        </Field>
        <Field label="New password" hint="12 to 128 characters." error={errors["new_password"]} required>
          <Input
            name="new-password"
            type="password"
            autoComplete="new-password"
            value={form.next}
            onChange={(event) => {
              setForm({ ...form, next: event.target.value });
            }}
          />
        </Field>
        <Field label="Repeat the new password" error={errors["confirm"]} required>
          <Input
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            value={form.confirm}
            onChange={(event) => {
              setForm({ ...form, confirm: event.target.value });
            }}
          />
        </Field>
        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <div className="auth-actions">
          <Button type="submit" variant="primary" loading={busy}>
            Change password
          </Button>
        </div>
      </form>
    </Card>
  );
}
