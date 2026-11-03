// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The signed-in user as the auth pages see it: a typed, validated view of
 * `GET /auth/session` (lib/session.ts keeps the raw state), plus the steps
 * every login, logout, step-up and household switch share.
 */
import { useQuery, type QueryClient } from "@tanstack/react-query";

import type { components } from "../../lib/api/schema";
import { csrfStore, type SessionState } from "../../lib/csrf";
import { queryRoots } from "../../lib/queryKeys";
import { sessionQueryOptions } from "../../lib/session";

export type Role = components["schemas"]["HouseholdRole"];
export type AuthSession = components["schemas"]["SessionOut"];

const ROLES: readonly Role[] = ["owner", "admin", "member", "viewer"];
const AUTH_METHODS: readonly AuthSession["auth_method"][] = ["local", "oidc", "breakglass", "bootstrap", "token"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value);
}

function isAuthMethod(value: unknown): value is AuthSession["auth_method"] {
  return typeof value === "string" && (AUTH_METHODS as readonly string[]).includes(value);
}

/** The typed session of an `authenticated` state, or `null` if malformed or absent. */
export function authSessionOf(state: SessionState | undefined): AuthSession | null {
  if (state?.status !== "authenticated") {
    return null;
  }
  const { data } = state;
  const user = data["user"];
  const mfa = data["mfa"];
  const household = data["household"];
  if (!isRecord(user) || !isRecord(mfa)) {
    return null;
  }
  const id = str(user["id"]);
  const username = str(user["username"]);
  if (id === null || username === null) {
    return null;
  }
  let activeHousehold: AuthSession["household"] = null;
  if (isRecord(household)) {
    const householdId = str(household["id"]);
    const role = household["role"];
    if (householdId !== null && isRole(role)) {
      activeHousehold = { id: householdId, name: str(household["name"]) ?? "", role };
    }
  }
  const method = data["auth_method"];
  return {
    authenticated: true,
    user: {
      id,
      username,
      display_name: str(user["display_name"]) ?? username,
      is_instance_admin: user["is_instance_admin"] === true,
    },
    household: activeHousehold,
    csrf_token: state.csrfToken,
    mfa: {
      enrolled: mfa["enrolled"] === true,
      verified: mfa["verified"] === true,
      verified_at: str(mfa["verified_at"]),
      required: mfa["required"] === true,
      enrollment_required: mfa["enrollment_required"] === true,
    },
    auth_method: isAuthMethod(method) ? method : "local",
  };
}

/** The raw session state and, when signed in, the typed session. */
export function useAuthSession() {
  const query = useQuery(sessionQueryOptions());
  return { query, state: query.data, session: authSessionOf(query.data) };
}

/** Roles that may manage a household's members. */
export function canManageMembers(role: Role | null | undefined): boolean {
  return role === "owner" || role === "admin";
}

function isSessionQuery(queryKey: readonly unknown[]): boolean {
  const [first, second] = queryKey;
  return first === queryRoots.session[0] && second === queryRoots.session[1];
}

/**
 * The session changed (login, MFA, setup): keep the new CSRF token, drop
 * every cached answer that belonged to the old session and re-read the
 * session before anything navigates. Callers navigate right after, so the
 * dropped queries' observers unmount.
 */
export async function sessionChanged(queryClient: QueryClient, csrfToken?: string | null): Promise<SessionState> {
  if (typeof csrfToken === "string" && csrfToken.length > 0) {
    csrfStore.set(csrfToken);
  }
  queryClient.removeQueries({ predicate: (query) => !isSessionQuery(query.queryKey) });
  return queryClient.query({ ...sessionQueryOptions(), staleTime: 0 });
}

/** The session ended (logout, revoked): forget its token and every cached answer. */
export async function sessionEnded(queryClient: QueryClient): Promise<SessionState> {
  csrfStore.clear();
  queryClient.removeQueries({ predicate: (query) => !isSessionQuery(query.queryKey) });
  return queryClient.query({ ...sessionQueryOptions(), staleTime: 0 });
}

/**
 * The household changed on the same page: re-read the session, then reset
 * every other query so the visible ones refetch for the new household.
 */
export async function householdSwitched(queryClient: QueryClient, csrfToken: string): Promise<void> {
  csrfStore.set(csrfToken);
  await queryClient.query({ ...sessionQueryOptions(), staleTime: 0 });
  await queryClient.resetQueries({ predicate: (query) => !isSessionQuery(query.queryKey) });
}

/** A new CSRF token after a rotation that keeps the same user and household (step-up, factor added). */
export function sessionRotated(queryClient: QueryClient, csrfToken: string): void {
  csrfStore.set(csrfToken);
  void queryClient.invalidateQueries({ queryKey: queryRoots.session });
}
