// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type FlowReport = Schemas["FlowReport"];
export type CashflowReport = Schemas["CashflowReport"];
export type CashflowMonth = Schemas["CashflowMonth"];
export type TrendReport = Schemas["TrendReport"];
export type TrendRow = Schemas["TrendRow"];
export type ReportItem = Schemas["ReportItem"];
export type GroupBy = Schemas["GroupBy"];
export type Flow = Schemas["Flow"];
export type Money = Schemas["Money"];

/** What every report shares: the account filter and the review toggle. */
export interface ReportScope {
  /** An account id; "" for every account the person can see. */
  account: string;
  /** `null` leaves the choice to the household setting (the server's default). */
  pending: boolean | null;
}

export interface FlowParams extends ReportScope {
  flow: Flow;
  from: string;
  to: string;
  groupBy: GroupBy;
  /** Category roll-up level, 1 to 3; only used when grouping by category. */
  depth: number;
}

export interface CashflowParams extends ReportScope {
  from: string;
  to: string;
}

export interface TrendParams extends ReportScope {
  flow: Flow;
  /** `YYYY-MM`: the month compared with the one before and a year earlier. */
  month: string;
  /** Length of the monthly series, 1 to 60. */
  months: number;
  depth: number;
}

/** Depth has no effect unless categories are grouped; one value keeps one cache entry. */
const flowDepth = (params: FlowParams): number => (params.groupBy === "category" ? params.depth : 1);

// Keys sit under `queryRoots.reports`, so the live-update layer (lib/sse.ts)
// refreshes every report when a transaction, category or payee changes.
export const reportKeys = {
  flow: (params: FlowParams) => [...queryRoots.reports, "flow", params.flow, params.from, params.to, params.groupBy, flowDepth(params), params.account, params.pending],
  cashflow: (params: CashflowParams) => [...queryRoots.reports, "cashflow", params.from, params.to, params.account, params.pending],
  trends: (params: TrendParams) => [...queryRoots.reports, "trends", params.flow, params.month, params.months, params.depth, params.account, params.pending],
} as const;

const scopeQuery = (scope: ReportScope) => ({
  ...(scope.account === "" ? {} : { account: [scope.account] }),
  ...(scope.pending === null ? {} : { include_pending: scope.pending }),
});

export function getFlowReport(params: FlowParams): Promise<FlowReport> {
  const query = {
    date_from: params.from,
    date_to: params.to,
    group_by: params.groupBy,
    ...(params.groupBy === "category" ? { depth: params.depth } : {}),
    ...scopeQuery(params),
  };
  return params.flow === "spending"
    ? unwrap(api.GET("/api/v1/reports/spending", { params: { query } }))
    : unwrap(api.GET("/api/v1/reports/income", { params: { query } }));
}

export const getCashflowReport = (params: CashflowParams): Promise<CashflowReport> =>
  unwrap(api.GET("/api/v1/reports/cashflow", { params: { query: { date_from: params.from, date_to: params.to, ...scopeQuery(params) } } }));

export const getTrendReport = (params: TrendParams): Promise<TrendReport> =>
  unwrap(
    api.GET("/api/v1/reports/trends", {
      params: { query: { flow: params.flow, month: params.month, months: params.months, depth: params.depth, ...scopeQuery(params) } },
    }),
  );
