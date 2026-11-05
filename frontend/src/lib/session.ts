// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The current session as a TanStack query. Reading it also refreshes the
 * CSRF token store (lib/csrf.ts). The session endpoint arrives in Phase 1;
 * until then the state is `unavailable` and nothing depends on it.
 */
import { queryOptions } from "@tanstack/react-query";

import { csrfStore, type SessionState } from "./csrf";
import { queryRoots } from "./queryKeys";

export function sessionQueryOptions() {
  return queryOptions<SessionState>({
    queryKey: queryRoots.session,
    queryFn: () => csrfStore.refresh(),
    staleTime: 5 * 60_000,
    retry: false,
    refetchOnWindowFocus: true,
  });
}
