// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Prove a second factor: a security key or passkey (WebAuthn), a code from
 * an authenticator app (TOTP) or a recovery code. Used by the login's second
 * step and by the step-up dialog; the caller decides where the proof goes.
 */
import { useId, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { webauthnAuthenticationOptions, type MfaProof } from "./api";
import { authErrorMessage } from "./errors";
import { signChallenge, webauthnSupported } from "./webauthn";

export interface MfaProofFormProps {
  /** Factor types the user has ("totp", "webauthn"). */
  methods: readonly string[];
  /** Send the proof; throw to show an error. */
  submit: (proof: MfaProof) => Promise<void>;
  /** Label of the TOTP and recovery submit buttons. */
  submitLabel?: string;
}

type Mode = "factor" | "recovery";

function normaliseCode(value: string): string {
  return value.replace(/[\s-]/g, "");
}

export function MfaProofForm({ methods, submit, submitLabel = "Verify" }: MfaProofFormProps) {
  const hasTotp = methods.includes("totp");
  const hasWebauthn = methods.includes("webauthn");
  const canUseWebauthn = hasWebauthn && webauthnSupported();
  const [mode, setMode] = useState<Mode>(hasTotp || canUseWebauthn ? "factor" : "recovery");
  const [code, setCode] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [busy, setBusy] = useState<"webauthn" | "code" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const errorId = useId();

  const run = async (kind: "webauthn" | "code", makeProof: () => Promise<MfaProof>): Promise<void> => {
    setBusy(kind);
    setError(null);
    try {
      await submit(await makeProof());
    } catch (caught) {
      setError(authErrorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const onWebauthn = (): void => {
    void run("webauthn", async () => {
      const options = await webauthnAuthenticationOptions();
      return { method: "webauthn", credential: await signChallenge(options) };
    });
  };

  const onTotp = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const value = normaliseCode(code);
    if (!/^\d{6,8}$/.test(value)) {
      setError("Enter the 6-digit code from your authenticator app.");
      return;
    }
    void run("code", () => Promise.resolve({ method: "totp", code: value }));
  };

  const onRecovery = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const value = recoveryCode.trim();
    if (normaliseCode(value).length < 16) {
      setError("Enter one of your recovery codes, for example ABCD-EFGH-JKLM-NPQR.");
      return;
    }
    void run("code", () => Promise.resolve({ method: "recovery", code: value }));
  };

  const errorText =
    error === null ? null : (
      <p id={errorId} className="auth-error" role="alert">
        {error}
      </p>
    );

  if (mode === "recovery") {
    return (
      <div className="auth-stack">
        <form className="auth-stack" onSubmit={onRecovery} noValidate aria-describedby={error === null ? undefined : errorId}>
          <Field label="Recovery code" hint="Each recovery code works once.">
            <Input
              name="recovery-code"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              autoFocus
              value={recoveryCode}
              onChange={(event) => {
                setRecoveryCode(event.target.value);
              }}
            />
          </Field>
          {errorText}
          <Button type="submit" variant="primary" loading={busy === "code"}>
            {submitLabel}
          </Button>
        </form>
        {hasTotp || canUseWebauthn ? (
          <Button
            variant="ghost"
            onClick={() => {
              setMode("factor");
              setError(null);
            }}
          >
            Use your security key or authenticator app
          </Button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="auth-stack">
      {canUseWebauthn ? (
        <Button variant={hasTotp ? "secondary" : "primary"} loading={busy === "webauthn"} disabled={busy === "code"} onClick={onWebauthn}>
          Use security key or passkey
        </Button>
      ) : null}
      {hasWebauthn && !canUseWebauthn ? (
        <p className="auth-muted">This browser cannot use security keys. Use another method.</p>
      ) : null}
      {hasTotp ? (
        <form className="auth-stack" onSubmit={onTotp} noValidate aria-describedby={error === null ? undefined : errorId}>
          <Field label="Authentication code" hint="The 6-digit code from your authenticator app.">
            <Input
              name="totp-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus={!canUseWebauthn}
              maxLength={10}
              value={code}
              onChange={(event) => {
                setCode(event.target.value);
              }}
            />
          </Field>
          <Button type="submit" variant="primary" loading={busy === "code"} disabled={busy === "webauthn"}>
            {submitLabel}
          </Button>
        </form>
      ) : null}
      {errorText}
      <Button
        variant="ghost"
        onClick={() => {
          setMode("recovery");
          setError(null);
        }}
      >
        Use a recovery code
      </Button>
    </div>
  );
}
