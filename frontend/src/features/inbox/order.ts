// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The order of the Inbox (spec section 4.2). Documents whose money is wrong
 * until someone answers come first, then documents that cannot import until
 * someone answers, then documents with something to note; the oldest
 * document first inside each group. Subscription cards follow: one per
 * series, the series with an open change first (newest change first), then
 * the suggested series with none (most charges first).
 *
 * Types only are imported, so a page that needs the order (Home) does not
 * pull in the review or subscription code.
 */
import type { CardAction, ReviewCard } from "../review/api";
import type { Subscription, SubscriptionEvent } from "../subscriptions/api";

export type Action = CardAction;

/** The group of each action the server names; an action not listed goes in group 3. */
export const ACTION_GROUP: ReadonlyMap<Action, 1 | 2 | 3> = new Map<Action, 1 | 2 | 3>([
  ["reconcile", 1],
  ["check_rows", 1],
  ["confirm_duplicate", 1],
  ["answer_sign", 1],
  ["choose_account", 2],
  ["map_columns", 2],
  ["choose_owner", 2],
  ["teach", 2],
  ["reprocess", 2],
  ["attach_receipt", 3],
  ["acknowledge", 3],
]);

function groupOf(card: ReviewCard): number {
  return ACTION_GROUP.get(card.action) ?? 3;
}

function stamp(iso: string): number {
  const time = Date.parse(iso);
  return Number.isNaN(time) ? 0 : time;
}

/** The document cards in the Inbox's order; a new array, the one given is left as it was. */
export function orderCards(cards: readonly ReviewCard[]): ReviewCard[] {
  return [...cards].sort(
    (a, b) => groupOf(a) - groupOf(b) || stamp(a.document.created_at) - stamp(b.document.created_at),
  );
}

/** One subscription card: a series and its open changes, newest first. */
export interface SubscriptionCardEntry {
  /** The series' id (`schedule_id` of its changes). */
  id: string;
  name: string;
  /** The series when it is suggested; `null` for a series known from its open changes only. */
  series: Subscription | null;
  events: SubscriptionEvent[];
}

/**
 * One entry per series that is suggested or has an open change, each once,
 * as `GET /inbox/summary` counts them.
 */
export function subscriptionCards(
  suggested: readonly Subscription[],
  events: readonly SubscriptionEvent[],
): SubscriptionCardEntry[] {
  const byId = new Map<string, SubscriptionCardEntry>();
  for (const row of suggested) {
    byId.set(row.id, { id: row.id, name: row.name, series: row, events: [] });
  }
  for (const change of events) {
    const entry = byId.get(change.schedule_id);
    if (entry === undefined) {
      byId.set(change.schedule_id, { id: change.schedule_id, name: change.name, series: null, events: [change] });
    } else {
      entry.events.push(change);
    }
  }
  const newestFirst = (a: SubscriptionEvent, b: SubscriptionEvent): number =>
    b.occurred_on.localeCompare(a.occurred_on);
  const entries = [...byId.values()].map((entry) => ({ ...entry, events: [...entry.events].sort(newestFirst) }));
  const changed = entries
    .filter((entry) => entry.events.length > 0)
    .sort((a, b) => (b.events[0]?.occurred_on ?? "").localeCompare(a.events[0]?.occurred_on ?? ""));
  const quiet = entries
    .filter((entry) => entry.events.length === 0)
    .sort((a, b) => (b.series?.occurrences ?? 0) - (a.series?.occurrences ?? 0));
  return [...changed, ...quiet];
}
