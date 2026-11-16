// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The reads of the Inbox's own cards (spec section 4.1). The document cards
 * come from `review/api.ts`; the writes go through the frozen modules of the
 * other sections (`subscriptions/api.ts`, `transactions/api.ts`).
 */
import { api, unwrap } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import type { LearnedCategory } from "../categories/api";
import type { Subscription } from "../subscriptions/api";
import type { Transaction } from "../transactions/api";

/** How many suggested rows the suggestions card reads; more than this and it says "the five most common of N". */
export const SUGGESTIONS_LIMIT = 200;

/** Learned entries read for "Make it a rule": the first page only. */
const LEARNED_LIMIT = 100;

export const inboxPageKeys = {
  /** Under the subscriptions root, so a decision anywhere refreshes it. */
  suggestedSeries: [...queryRoots.subscriptions, "inbox", "suggested"],
  /** Under the transactions root, so every change to the ledger refreshes them. */
  suggestedRows: [...queryRoots.transactions, "inbox", "suggested"],
  ruleOffers: [...queryRoots.transactions, "inbox", "rule-offers"],
} as const;

/** `GET /subscriptions?status=suggested`: the series PerFin thinks repeat, waiting for a person to say so. */
export async function listSuggestedSeries(): Promise<Subscription[]> {
  const list = await unwrap(api.GET("/api/v1/subscriptions", { params: { query: { status: ["suggested"] } } }));
  return list.items;
}

/** `GET /transactions?suggested=true&limit=200`: rows with a suggestion and no category, newest first. */
export async function listSuggestedRows(): Promise<Transaction[]> {
  const page = await unwrap(
    api.GET("/api/v1/transactions", { params: { query: { suggested: true, limit: SUGGESTIONS_LIMIT } } }),
  );
  return page.items;
}

/** `GET /categorisation/learned`, first page: the entries worth offering as a rule. */
export async function listRuleOffers(): Promise<LearnedCategory[]> {
  const page = await unwrap(api.GET("/api/v1/categorisation/learned", { params: { query: { limit: LEARNED_LIMIT } } }));
  return page.items.filter(
    (entry) => entry.offer_rule && entry.rule_id === null && entry.example_transaction_id !== null,
  );
}
