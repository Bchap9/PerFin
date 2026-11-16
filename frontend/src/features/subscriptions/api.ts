// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Subscriptions and bills: the list with its totals, one series up close, the
 * open changes, a person's decision about a series and "seen" for its changes.
 */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Subscription = Schemas["SubscriptionOut"];
export type SubscriptionList = Schemas["SubscriptionList"];
export type SubscriptionTotals = Schemas["SubscriptionTotals"];
export type SubscriptionDetail = Schemas["SubscriptionDetail"];
export type SubscriptionCharge = Schemas["SubscriptionCharge"];
export type SubscriptionEvent = Schemas["SubscriptionEventOut"];
export type SubscriptionDecision = Schemas["SubscriptionDecision"];
export type SubscriptionStatus = Subscription["status"];
export type SubscriptionKind = Subscription["kind"];

export const subscriptionKeys = {
  list: [...queryRoots.subscriptions, "list"],
  openEvents: [...queryRoots.subscriptions, "events", "open"],
  detail: (id: string) => [...queryRoots.subscriptions, "detail", id],
} as const;

/** What a decision changes on screen: this view and the calendar's recurring items. */
export const SUBSCRIPTION_FAMILIES = [queryRoots.subscriptions, queryRoots.recurring] as const;

const EVENTS_PAGE = 200;

export const listSubscriptions = (): Promise<SubscriptionList> => unwrap(api.GET("/api/v1/subscriptions", {}));

/** One series with its linked charges and everything that changed about it. */
export const getSubscription = (id: string): Promise<SubscriptionDetail> =>
  unwrap(api.GET("/api/v1/subscriptions/{schedule_id}", { params: { path: { schedule_id: id } } }));

/** Every change nobody has looked at yet, newest first. */
export function listOpenEvents(): Promise<SubscriptionEvent[]> {
  return collectPages((cursor) =>
    unwrap(
      api.GET("/api/v1/subscriptions/events", {
        params: { query: { state: "open", limit: EVENTS_PAGE, ...(cursor === null ? {} : { cursor }) } },
      }),
    ),
  );
}

export const decide = (id: string, body: SubscriptionDecision): Promise<Subscription> =>
  unwrap(api.POST("/api/v1/subscriptions/{schedule_id}/decision", { params: { path: { schedule_id: id } }, body }));

export const markEventsSeen = (ids: string[]) => unwrap(api.POST("/api/v1/subscriptions/events/seen", { body: { ids } }));
