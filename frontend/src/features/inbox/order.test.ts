// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReviewCard } from "../review/api";
import type { Subscription, SubscriptionEvent } from "../subscriptions/api";
import { ACTION_GROUP, orderCards, subscriptionCards } from "./order";

function card(id: string, action: string, createdAt: string): ReviewCard {
  return {
    action: action as ReviewCard["action"],
    action_item_id: `item-${id}`,
    document: {
      id,
      created_at: createdAt,
      kind: "statement",
      mime_detected: "text/csv",
      original_filename: `${id}.csv`,
      period_end: null,
      period_start: null,
      previewable: false,
      source: "upload",
      status: "needs_review",
    },
    items: [],
    notices: 0,
    open: 1,
  };
}

const money = (amount: string) => ({ amount, currency: "USD" });

function series(id: string, extra: Partial<Subscription> = {}): Subscription {
  return {
    id,
    account_id: "a-1",
    amount: money("-9.99"),
    amount_max: money("-9.99"),
    amount_min: money("-9.99"),
    cadence: "monthly",
    category_id: null,
    confidence: null,
    first_seen_on: "2026-01-05",
    interval_count: 1,
    kind: "subscription",
    kind_source: "detector",
    last_seen_on: "2026-10-05",
    merchant: null,
    monthly_cost: money("9.99"),
    name: `Series ${id}`,
    next_due: "2026-11-05",
    occurrences: 3,
    open_events: 0,
    origin: "detected",
    overdue: false,
    payee_id: null,
    price_change: null,
    status: "suggested",
    status_source: "detector",
    yearly_cost: money("119.88"),
    ...extra,
  };
}

function event(id: string, scheduleId: string, occurredOn: string): SubscriptionEvent {
  return {
    id,
    schedule_id: scheduleId,
    name: `Series ${scheduleId}`,
    kind: "price_changed",
    occurred_on: occurredOn,
    amount_before: money("-9.99"),
    amount_after: money("-11.99"),
    state: "open",
    transaction_id: null,
  };
}

describe("orderCards", () => {
  it("puts the three groups in order, oldest first inside a group, unknown actions last", () => {
    const cards = [
      card("note-old", "acknowledge", "2026-10-01T09:00:00Z"),
      card("unknown", "something_new", "2026-09-01T09:00:00Z"),
      card("block-new", "map_columns", "2026-11-02T09:00:00Z"),
      card("money-new", "check_rows", "2026-11-03T09:00:00Z"),
      card("receipt", "attach_receipt", "2026-08-01T09:00:00Z"),
      card("block-old", "choose_account", "2026-10-15T09:00:00Z"),
      card("money-old", "reconcile", "2026-10-20T09:00:00Z"),
    ];
    const before = cards.map((one) => one.document.id);
    expect(orderCards(cards).map((one) => one.document.id)).toEqual([
      "money-old",
      "money-new",
      "block-old",
      "block-new",
      "receipt",
      "unknown",
      "note-old",
    ]);
    // The list given is left as it was.
    expect(cards.map((one) => one.document.id)).toEqual(before);
  });

  it("names a group for every action the server knows", () => {
    expect(ACTION_GROUP.get("reconcile")).toBe(1);
    expect(ACTION_GROUP.get("check_rows")).toBe(1);
    expect(ACTION_GROUP.get("confirm_duplicate")).toBe(1);
    expect(ACTION_GROUP.get("answer_sign")).toBe(1);
    for (const action of ["choose_account", "map_columns", "choose_owner", "teach", "reprocess"] as const) {
      expect(ACTION_GROUP.get(action)).toBe(2);
    }
    expect(ACTION_GROUP.get("attach_receipt")).toBe(3);
    expect(ACTION_GROUP.get("acknowledge")).toBe(3);
  });
});

describe("subscriptionCards", () => {
  it("lists series with a change first, newest change first, then suggested series by charges", () => {
    const suggested = [
      series("s-few", { occurrences: 2 }),
      series("s-many", { occurrences: 9 }),
      series("s-changed", { occurrences: 4, open_events: 1 }),
    ];
    const events = [
      event("e-1", "s-active", "2026-10-01"),
      event("e-2", "s-changed", "2026-11-01"),
      event("e-3", "s-active", "2026-10-20"),
    ];
    const cards = subscriptionCards(suggested, events);
    expect(cards.map((one) => one.id)).toEqual(["s-changed", "s-active", "s-many", "s-few"]);
    // A suggested series with an open change is one card, holding its change.
    expect(cards[0]?.series?.id).toBe("s-changed");
    expect(cards[0]?.events.map((one) => one.id)).toEqual(["e-2"]);
    // A series that is not suggested is known from its changes only.
    expect(cards[1]?.series).toBeNull();
    expect(cards[1]?.name).toBe("Series s-active");
    expect(cards[1]?.events.map((one) => one.id)).toEqual(["e-3", "e-1"]);
    expect(cards[2]?.events).toEqual([]);
  });

  it("gives nothing when nothing waits", () => {
    expect(subscriptionCards([], [])).toEqual([]);
  });
});
