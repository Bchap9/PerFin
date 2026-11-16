// SPDX-License-Identifier: AGPL-3.0-or-later
/** The calls of Home: each panel has its own query, so one failing call does not blank the page. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { listSchedules, type Schedule } from "../calendar/api";
import { listOpenEvents, listSubscriptions, type Subscription, type SubscriptionEvent } from "../subscriptions/api";

type Schemas = components["schemas"];

export type FlowReport = Schemas["FlowReport"];
export type NetWorthHistory = Schemas["NetWorthHistory"];

export const homeKeys = {
  spending: (from: string, to: string, previousFrom: string, previousTo: string) => [
    ...queryRoots.reports,
    "home-spending",
    from,
    to,
    previousFrom,
    previousTo,
  ],
  netWorthSeries: [...queryRoots.netWorth, "home-series"],
  comingUp: [...queryRoots.subscriptions, "home-coming-up"],
  seriesNeedingYou: [...queryRoots.subscriptions, "home-needs-you"],
  latest: [...queryRoots.transactions, "home-latest"],
} as const;

const getSpendingByCategory = (from: string, to: string) =>
  unwrap(api.GET("/api/v1/reports/spending", { params: { query: { date_from: from, date_to: to, group_by: "category", depth: 1 } } }));

/** Spending by top-level category for this month so far and for the same days of last month. */
export async function getSpendingComparison(
  from: string,
  to: string,
  previousFrom: string,
  previousTo: string,
): Promise<{ current: FlowReport; previous: FlowReport }> {
  const [current, previous] = await Promise.all([getSpendingByCategory(from, to), getSpendingByCategory(previousFrom, previousTo)]);
  return { current, previous };
}

/** Net worth at every month end of the last year, and today. */
export const getNetWorthSeries = (): Promise<NetWorthHistory> =>
  unwrap(api.GET("/api/v1/networth", { params: { query: { range: "1y", granularity: "month" } } }));

/** The subscriptions and the other recurring items, for the charges coming up. */
export async function getComingUp(): Promise<{ subscriptions: Subscription[]; recurring: Schedule[] }> {
  const [subscriptions, recurring] = await Promise.all([listSubscriptions(), listSchedules()]);
  return { subscriptions: subscriptions.items, recurring };
}

export const getRecentTransactions = (limit = 5) => unwrap(api.GET("/api/v1/transactions", { params: { query: { limit } } }));

/** The suggested series and every open change, for the subscription rows of Needs you. */
export async function getSeriesNeedingYou(): Promise<{ suggested: Subscription[]; events: SubscriptionEvent[] }> {
  const [list, events] = await Promise.all([listSubscriptions(), listOpenEvents()]);
  return { suggested: list.items.filter((row) => row.status === "suggested"), events };
}
