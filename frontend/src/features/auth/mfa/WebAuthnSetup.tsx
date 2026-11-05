// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Register a security key or passkey: registration options from the server
 * (`POST /auth/mfa/webauthn/registration-options`, a stored challenge), the
 * browser's WebAuthn ceremony, then `POST /auth/mfa/webauthn`. The whole
 * sequence is retried after step-up when the server asks for it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { registerWebauthn, webauthnRegistrationOptions, type FactorAdded } from "../api";
import { authErrorMessage } from "../errors";
import { sessionRotated } from "../session";
import { isStepUpCancelled, useStepUp } from "../StepUp";
import { createCredential, webauthnSupported } from "../webauthn";

export interface WebAuthnSetupProps {
  onAdded: (result: FactorAdded) => void;
}

export function WebAuthnSetup({ onAdded }: WebAuthnSetupProps) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const [name, setName] = useState("Security key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!webauthnSupported()) {
    return <p className="auth-muted">This browser cannot register security keys or passkeys.</p>;
  }

  const register = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const label = name.trim() || "Security key";
      const added = await runWithStepUp(async () => {
        const options = await webauthnRegistrationOptions();
        const credential = await createCredential(options);
        return registerWebauthn(credential, label);
      });
      sessionRotated(queryClient, added.csrf_token);
      setName("Security key");
      onAdded(added);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(authErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="auth-inline-form"
      noValidate
      onSubmit={(event) => {
        void register(event);
      }}
    >
      <Field label="Key name" hint="For example, the key's colour or the device it is on.">
        <Input
          name="webauthn-name"
          maxLength={100}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Button type="submit" variant="primary" loading={busy}>
        Add security key or passkey
      </Button>
      {error === null ? null : (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
