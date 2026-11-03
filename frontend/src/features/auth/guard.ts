// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The route guard (docs/AUTH.md): every page except the login, setup and
 * password-reset pages needs a session. It runs in the root route's
 * `beforeLoad` and sends the browser to
 *
 * - `/login?return_to=<here>` when nobody is signed in;
 * - `/login/mfa?return_to=<here>` while a password login waits for its
 *   second factor;
 * - `/login/enroll-mfa?return_to=<here>` while `PERFIN_REQUIRE_MFA` restricts
 *   the session to enrolling a first factor;
 * - the `return_to` target when a signed-in user opens the login page.
 *
 * When the session endpoint cannot be reached (`unavailable`) nothing is
 * redirected: the page renders and shows its own errors. The server enforces
 * every rule again; the guard only spares the user dead ends.
 */
import { redirect, type ParsedLocation } from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";

import type { SessionState } from "../../lib/csrf";
import { sessionQueryOptions } from "../../lib/session";
import { returnToFromSearch, withReturnTo } from "./returnTo";
import { authSessionOf } from "./session";

export const LOGIN_PATH = "/login";
export const MFA_PATH = "/login/mfa";
export const ENROLL_PATH = "/login/enroll-mfa";
export const LINK_OIDC_PATH = "/login/link-oidc";
export const SETUP_PATH = "/setup";
export const FORGOT_PATH = "/forgot-password";
export const RESET_PATH = "/reset-password";

/** Pages reachable without a session. */
const PUBLIC_PATHS = new Set([LOGIN_PATH, MFA_PATH, LINK_OIDC_PATH, SETUP_PATH, FORGOT_PATH, RESET_PATH]);

function normalise(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, "");
  return trimmed === "" ? "/" : trimmed;
}

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(normalise(pathname));
}

/** Pages shown without the navigation chrome (the public pages plus enrolment). */
export function isBarePath(pathname: string): boolean {
  const path = normalise(pathname);
  return PUBLIC_PATHS.has(path) || path === ENROLL_PATH;
}

/**
 * Where the guard sends the browser, or `null` to stay. `href` is the
 * requested path with its query (the `return_to` target).
 */
export function guardRedirect(state: SessionState, pathname: string, search: string, href: string): string | null {
  const path = normalise(pathname);
  switch (state.status) {
    case "unavailable":
      return null;
    case "anonymous":
      return isPublicPath(path) ? null : withReturnTo(LOGIN_PATH, href);
    case "mfa_pending":
      return isPublicPath(path) ? null : withReturnTo(MFA_PATH, href);
    case "authenticated": {
      const session = authSessionOf(state);
      if (session?.mfa.enrollment_required === true) {
        if (path === ENROLL_PATH || isPublicPath(path)) {
          return path === LOGIN_PATH || path === MFA_PATH ? withReturnTo(ENROLL_PATH, returnToFromSearch(search)) : null;
        }
        return withReturnTo(ENROLL_PATH, href);
      }
      if (path === LOGIN_PATH || path === MFA_PATH || path === ENROLL_PATH) {
        return returnToFromSearch(search);
      }
      return null;
    }
  }
}

/** The root route's `beforeLoad`. */
export async function authGuard({
  context,
  location,
}: {
  context: { queryClient: QueryClient };
  location: ParsedLocation;
}): Promise<void> {
  const state = await context.queryClient.query(sessionQueryOptions());
  const target = guardRedirect(state, location.pathname, location.searchStr, location.href);
  if (target !== null) {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- the router's redirect protocol
    throw redirect({ href: target, replace: true });
  }
}
