// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The header's account area: "Sign in" when nobody is, otherwise the
 * household switcher (when the user belongs to more than one household), the
 * user's name and "Sign out". Switching household rotates the session
 * (`POST /auth/session/household`) and refetches everything on screen.
 * Signing out of an OIDC session also ends it at the identity provider when
 * the server returns an end-session URL.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { isApiError } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { listHouseholds, logout, oidcLogout, switchHousehold } from "./api";
import { useGoTo } from "./AuthLayout";
import { authErrorMessage } from "./errors";
import { LOGIN_PATH } from "./guard";
import { householdSwitched, sessionEnded, useAuthSession, type AuthSession } from "./session";

export const householdsKey = [...queryRoots.settings, "households"] as const;

/** An absolute http(s) URL the server gave us to navigate to, or `null`. */
export function safeExternalUrl(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function HouseholdSwitcher({ session }: { session: AuthSession }) {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const selectId = useId();
  const [busy, setBusy] = useState(false);
  const households = useQuery({ queryKey: householdsKey, queryFn: listHouseholds, staleTime: 60_000 });
  const mine = (households.data ?? []).filter((household) => household.role !== null);
  const current = session.household;

  if (current === null) {
    return null;
  }
  if (mine.length < 2) {
    return <span className="account-menu__household">{current.name}</span>;
  }

  const onChange = async (householdId: string): Promise<void> => {
    if (householdId === current.id) {
      return;
    }
    setBusy(true);
    try {
      const switched = await switchHousehold(householdId);
      await householdSwitched(queryClient, switched.csrf_token);
      notify({ tone: "success", title: `Switched to ${switched.household.name}` });
    } catch (caught) {
      notify({ tone: "error", title: "Could not switch household", description: authErrorMessage(caught) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="account-menu__household">
      <label htmlFor={selectId} className="visually-hidden">
        Household
      </label>
      <span className="select">
        <select
          id={selectId}
          className="select__control select__control--compact"
          value={current.id}
          disabled={busy}
          onChange={(event) => {
            void onChange(event.target.value);
          }}
        >
          {mine.map((household) => (
            <option key={household.id} value={household.id}>
              {household.name}
            </option>
          ))}
        </select>
      </span>
    </span>
  );
}

export function useSignOut(): { signOut: () => Promise<void>; busy: boolean } {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const { session } = useAuthSession();
  const { notify } = useToast();
  const [busy, setBusy] = useState(false);
  const signOut = async (): Promise<void> => {
    setBusy(true);
    let redirectUrl: string | null = null;
    try {
      if (session?.auth_method === "oidc") {
        try {
          redirectUrl = safeExternalUrl(await oidcLogout());
        } catch (caught) {
          // OIDC was switched off since this session began: a plain logout.
          if (!(isApiError(caught) && caught.status === 404)) {
            throw caught;
          }
          await logout();
        }
      } else {
        await logout();
      }
    } catch (caught) {
      notify({ tone: "error", title: "Sign-out failed", description: authErrorMessage(caught) });
      setBusy(false);
      return;
    }
    await sessionEnded(queryClient);
    if (redirectUrl !== null) {
      window.location.assign(redirectUrl);
      return;
    }
    setBusy(false);
    await goTo(LOGIN_PATH, { replace: true });
  };
  return { signOut, busy };
}

export function AccountMenu() {
  const { state, session } = useAuthSession();
  const { signOut, busy } = useSignOut();

  if (state?.status === "anonymous") {
    return (
      <Link to={LOGIN_PATH} className="btn btn--secondary btn--sm">
        Sign in
      </Link>
    );
  }
  if (session === null) {
    return null;
  }
  return (
    <div className="account-menu">
      <HouseholdSwitcher session={session} />
      <Link to="/settings/profile" className="account-menu__user" title={`Signed in as ${session.user.username}`}>
        {session.user.display_name}
      </Link>
      <Button
        size="sm"
        variant="secondary"
        loading={busy}
        onClick={() => {
          void signOut();
        }}
      >
        Sign out
      </Button>
    </div>
  );
}
