// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";

export const dashboardKeys = {
  cashflow: (from: string, to: string) => [...queryRoots.reports, "dashboard-cashflow", from, to],
  spending: (from: string, to: string) => [...queryRoots.reports, "dashboard-spending", from, to],
  recent: [...queryRoots.transactions, "dashboard-recent"],
  review: [...queryRoots.review, "dashboard-open"],
} as const;

export const getCashflow = (from: string, to: string) =>
  unwrap(api.GET("/api/v1/reports/cashflow", { params: { query: { date_from: from, date_to: to } } }));

export const getSpendingByCategory = (from: string, to: string) =>
  unwrap(api.GET("/api/v1/reports/spending", { params: { query: { date_from: from, date_to: to, group_by: "category", depth: 1 } } }));

export const getRecentTransactions = (limit = 8) =>
  unwrap(api.GET("/api/v1/transactions", { params: { query: { limit } } }));

/** Open review items; the page size caps the count, `more` says there are further pages. */
export async function getOpenReviewCount(): Promise<{ count: number; more: boolean }> {
  const page = await unwrap(api.GET("/api/v1/review", { params: { query: { state: "open", limit: 100 } } }));
  return { count: page.items.length, more: page.next_cursor !== null };
}
