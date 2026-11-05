// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The login's second step: the password (or the IdP) was accepted and the
 * session is `mfa_pending` until `POST /auth/mfa/verify` accepts a factor.
 * The verification rotates the session; then the browser continues to the
 * safe `return_to` target.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { logout, verifyMfa, type MfaProof } from "./api";
import { AuthLayout, useGoTo, useLocationParts } from "./AuthLayout";
import { LOGIN_PATH } from "./guard";
import { MfaProofForm } from "./MfaProofForm";
import { returnToFromSearch, withReturnTo } from "./returnTo";
import { sessionChanged, sessionEnded, useAuthSession } from "./session";

const LOW_RECOVERY_CODES = 3;

export function MfaPage() {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const { notify } = useToast();
  const { search } = useLocationParts();
  const returnTo = returnToFromSearch(search);
  const { query, state } = useAuthSession();
  const [leaving, setLeaving] = useState(false);
  const [done, setDone] = useState(false);

  if (query.isPending || done) {
    return (
      <AuthLayout title="Verify it is you">
        <Spinner label="Loading" />
      </AuthLayout>
    );
  }

  if (state?.status !== "mfa_pending") {
    return (
      <AuthLayout title="Verify it is you">
        <p role="status">This sign-in has expired or was already completed.</p>
        <Link to={withReturnTo(LOGIN_PATH, returnTo)} className="btn btn--primary btn--md">
          Sign in again
        </Link>
      </AuthLayout>
    );
  }

  const submit = async (proof: MfaProof): Promise<void> => {
    const verified = await verifyMfa(proof);
    setDone(true);
    await sessionChanged(queryClient, verified.csrf_token);
    const remaining = verified.recovery_codes_remaining;
    if (verified.method === "recovery" && typeof remaining === "number" && remaining < LOW_RECOVERY_CODES) {
      notify({
        tone: "warning",
        title: `${String(remaining)} recovery codes left`,
        description: "Generate new ones under Settings, Security.",
        durationMs: null,
      });
    }
    await goTo(returnTo, { replace: true });
  };

  const cancel = async (): Promise<void> => {
    setLeaving(true);
    try {
      await logout();
    } catch {
      // The pending session expires on its own; leave anyway.
    }
    await sessionEnded(queryClient);
    await goTo(LOGIN_PATH, { replace: true });
  };

  return (
    <AuthLayout title="Verify it is you" intro={<p>Your password was accepted. Confirm with your second factor.</p>}>
      <MfaProofForm methods={state.methods} submit={submit} submitLabel="Verify" />
      <Button
        variant="ghost"
        loading={leaving}
        onClick={() => {
          void cancel();
        }}
      >
        Cancel and sign out
      </Button>
    </AuthLayout>
  );
}
