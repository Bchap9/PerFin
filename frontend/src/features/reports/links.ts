// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Where a report row leads on the Transactions page, which reads `account`,
 * `category` (an id, or "none" for uncategorised), `date_from` and `date_to`
 * from its address (named like the filters of `GET /transactions`).
 */
import type { FlowReport, ReportItem } from "./api";

export type TransactionsSearch = Record<string, string>;

export function transactionsSearch(
  report: Pick<FlowReport, "group_by" | "date_from" | "date_to">,
  item: Pick<ReportItem, "key">,
  account: string,
): TransactionsSearch | null {
  const dates = { date_from: report.date_from, date_to: report.date_to };
  if (report.group_by === "category") {
    return { ...(account === "" ? {} : { account }), category: item.key ?? "none", ...dates };
  }
  if (report.group_by === "account" && item.key !== null) {
    return { account: item.key, ...dates };
  }
  return null;
}
