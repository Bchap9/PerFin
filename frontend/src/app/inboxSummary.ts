// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What waits on a person: `GET /inbox/summary` (spec section 4.4). This is
 * the only module that fetches it; the rail badge, Home and the Inbox read
 * it from here. The live-update stream invalidates `queryRoots.inbox` on
 * document, review, subscription, schedule and transaction events.
 */
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../lib/api/client";
import type { components } from "../lib/api/schema";
import { queryRoots } from "../lib/queryKeys";

export type InboxSummary = components["schemas"]["InboxSummary"];

export const inboxKeys = {
  summary: [...queryRoots.inbox, "summary"],
} as const;

export function getInboxSummary(): Promise<InboxSummary> {
  return unwrap(api.GET("/api/v1/inbox/summary"));
}

export function useInboxSummary() {
  return useQuery({ queryKey: inboxKeys.summary, queryFn: getInboxSummary, staleTime: 30_000 });
}
