// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Step-up (SECURITY.md A6): sensitive actions need a second factor verified
 * in the last 10 minutes and answer `403 step-up-required` otherwise.
 *
 * `useStepUp()` returns `run(action)`: it calls `action`; if the server asks
 * for step-up, it opens the verification dialog and, once the factor is
 * verified (`POST /auth/step-up`, which rotates the session and its CSRF
 * token), calls `action` again and returns that result. Closing the dialog
 * rejects with {@link StepUpCancelledError}. Without a provider, `run` just
 * calls the action.
 *
 * An OIDC session without a PerFin factor is offered to step up by signing
 * in again at the identity provider (`/auth/oidc/start?reauth=true`, which forces a new IdP login).
 * That counts only when the instance trusts the provider's MFA and the
 * provider really authenticated again (its `auth_time`); the original
 * request is not retried, the user repeats it after returning.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useRouterState } from "@tanstack/react-router";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { Dialog } from "../../components/Dialog";
import { Spinner } from "../../components/Spinner";
import { isApiError } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { getMfaStatus, oidcStartHref, stepUp, type MfaProof } from "./api";
import { authErrorMessage } from "./errors";
import { MfaProofForm } from "./MfaProofForm";
import { safeReturnTo } from "./returnTo";
import { sessionRotated, useAuthSession } from "./session";

export type StepUpRun = <T>(action: () => Promise<T>) => Promise<T>;

export class StepUpCancelledError extends Error {
  constructor() {
    super("Verification cancelled.");
    this.name = "StepUpCancelledError";
  }
}

export function isStepUpCancelled(error: unknown): error is StepUpCancelledError {
  return error instanceof StepUpCancelledError;
}

export function isStepUpRequired(error: unknown): boolean {
  return isApiError(error) && error.status === 403 && error.slug === "step-up-required";
}

export const mfaStatusKey = [...queryRoots.session, "mfa"] as const;

const StepUpContext = createContext<StepUpRun | null>(null);

const passthrough: StepUpRun = (action) => action();

/** `run(action)` with step-up retry (see the module doc). */
export function useStepUp(): StepUpRun {
  return useContext(StepUpContext) ?? passthrough;
}

interface Waiter {
  resolve: () => void;
  reject: (error: unknown) => void;
}

/** "Sign in again at the identity provider" for OIDC sessions (see the module doc). */
function IdpReauthLink() {
  const { session } = useAuthSession();
  const here = useRouterState({ select: (state) => state.location.href });
  if (session?.auth_method !== "oidc") {
    return null;
  }
  return (
    <p>
      If your identity provider checks your second factor, you can instead{" "}
      <a href={oidcStartHref(safeReturnTo(here), { reauth: true })}>sign in again with your identity provider</a>{" "}
      and repeat the action.
    </p>
  );
}

function StepUpBody({ onVerified }: { onVerified: (proof: MfaProof) => Promise<void> }) {
  const status = useQuery({ queryKey: mfaStatusKey, queryFn: getMfaStatus, staleTime: 0 });
  if (status.isPending) {
    return <Spinner label="Loading your second factors" />;
  }
  if (status.isError) {
    return (
      <p className="auth-error" role="alert">
        {authErrorMessage(status.error)}
      </p>
    );
  }
  const methods = status.data.methods.map((method) => method.type);
  if (methods.length === 0) {
    return (
      <>
        <p>
          This needs a second factor, and your account has none yet. Set one up under{" "}
          <Link to="/settings/security">Security</Link> first.
        </p>
        <IdpReauthLink />
      </>
    );
  }
  return <MfaProofForm methods={[...new Set(methods)]} submit={onVerified} submitLabel="Verify and continue" />;
}

export function StepUpProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const waiters = useRef<Waiter[]>([]);
  const [open, setOpen] = useState(false);

  const settle = useCallback((outcome: { ok: true } | { ok: false; error: unknown }) => {
    const pending = waiters.current;
    waiters.current = [];
    setOpen(false);
    for (const waiter of pending) {
      if (outcome.ok) {
        waiter.resolve();
      } else {
        waiter.reject(outcome.error);
      }
    }
  }, []);

  const run = useCallback<StepUpRun>(async (action) => {
    try {
      return await action();
    } catch (error) {
      if (!isStepUpRequired(error)) {
        throw error;
      }
      await new Promise<void>((resolve, reject) => {
        waiters.current.push({ resolve, reject });
        setOpen(true);
      });
      return action();
    }
  }, []);

  const onVerified = useCallback(
    async (proof: MfaProof) => {
      const verified = await stepUp(proof);
      sessionRotated(queryClient, verified.csrf_token);
      settle({ ok: true });
    },
    [queryClient, settle],
  );

  const value = useMemo(() => run, [run]);

  return (
    <StepUpContext.Provider value={value}>
      {children}
      <Dialog
        open={open}
        size="sm"
        title="Confirm it is you"
        description="For your security, verify your second factor to continue."
        onClose={() => {
          settle({ ok: false, error: new StepUpCancelledError() });
        }}
      >
        {open ? <StepUpBody onVerified={onVerified} /> : null}
      </Dialog>
    </StepUpContext.Provider>
  );
}
