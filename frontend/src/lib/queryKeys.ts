// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Root TanStack Query keys, one per resource family.
 *
 * Features build their keys under these roots (`[...queryRoots.accounts, id]`)
 * so the SSE layer (lib/sse.ts) can invalidate a whole family with a prefix
 * match when the server reports a change.
 */
export const queryRoots = {
  session: ["auth", "session"],
  accounts: ["accounts"],
  transactions: ["transactions"],
  budgets: ["budgets"],
  investments: ["investments"],
  loans: ["loans"],
  netWorth: ["net-worth"],
  documents: ["documents"],
  review: ["review"],
  tax: ["tax"],
  jobs: ["jobs"],
  dashboard: ["dashboard"],
  settings: ["settings"],
  notifications: ["notifications"],
} as const satisfies Record<string, readonly string[]>;

export type QueryRoot = (typeof queryRoots)[keyof typeof queryRoots];
