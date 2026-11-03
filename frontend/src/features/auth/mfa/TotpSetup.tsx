// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Set up an authenticator app: `POST /auth/mfa/totp` returns the secret once,
 * shown as a QR code and as text; the first code confirms it
 * (`POST /auth/mfa/totp/confirm`, which rotates the session). Both calls go
 * through step-up when the user already has a factor.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { confirmTotpEnrollment, startTotpEnrollment, type FactorAdded, type TotpEnrollment } from "../api";
import { authErrorMessage, fieldErrorsOf } from "../errors";
import { sessionRotated } from "../session";
import { isStepUpCancelled, useStepUp } from "../StepUp";
import { groupedSecret, otpauthQrDataUrl } from "./qr";

export interface TotpSetupProps {
  onAdded: (result: FactorAdded) => void;
}

export function TotpSetup({ onAdded }: TotpSetupProps) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const [enrollment, setEnrollment] = useState<TotpEnrollment | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [name, setName] = useState("Authenticator app");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const headingId = useId();

  const start = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const started = await runWithStepUp(startTotpEnrollment);
      setEnrollment(started);
      try {
        setQr(await otpauthQrDataUrl(started.otpauth_uri));
      } catch {
        setQr(null);
      }
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(authErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const value = code.replace(/\s/g, "");
    if (!/^\d{6,8}$/.test(value)) {
      setFieldErrors({ code: "Enter the 6-digit code your app shows." });
      return;
    }
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const added = await runWithStepUp(() => confirmTotpEnrollment(value, name.trim() || "Authenticator app"));
      sessionRotated(queryClient, added.csrf_token);
      setEnrollment(null);
      setQr(null);
      setCode("");
      onAdded(added);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(authErrorMessage(caught));
        setFieldErrors(fieldErrorsOf(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  if (enrollment === null) {
    return (
      <div className="auth-stack">
        <Button
          variant="primary"
          loading={busy}
          onClick={() => {
            void start();
          }}
        >
          Set up authenticator app
        </Button>
        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <section className="auth-stack" aria-labelledby={headingId}>
      <h3 id={headingId} className="auth-subheading">
        Scan the code with your authenticator app
      </h3>
      {qr === null ? null : <img className="totp-qr" src={qr} width={200} height={200} alt="QR code for your authenticator app" />}
      <p>
        Can not scan it? Enter this key in the app instead:{" "}
        <code className="secret-value" aria-label="Setup key">
          {groupedSecret(enrollment.secret)}
        </code>
      </p>
      <form
        className="auth-stack"
        noValidate
        onSubmit={(event) => {
          void confirm(event);
        }}
      >
        <Field label="Code from the app" error={fieldErrors["code"]} required>
          <Input
            name="totp-confirm-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            maxLength={10}
            value={code}
            onChange={(event) => {
              setCode(event.target.value);
            }}
          />
        </Field>
        <Field label="Name" hint="To tell your factors apart." error={fieldErrors["name"]}>
          <Input
            name="totp-name"
            maxLength={100}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
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
            Confirm
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setEnrollment(null);
              setQr(null);
              setError(null);
            }}
          >
            Cancel
          </Button>
        </div>
      </form>
    </section>
  );
}
