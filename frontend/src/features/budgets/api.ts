// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Budget = Schemas["BudgetOut"];
export type BudgetCreate = Schemas["BudgetCreate"];
export type BudgetUpdate = Schemas["BudgetUpdate"];
export type BudgetReport = Schemas["BudgetReportOut"];
export type BudgetReportLine = Schemas["BudgetReportLine"];

export const budgetKeys = {
  list: [...queryRoots.budgets, "list"],
  one: (id: string) => [...queryRoots.budgets, "one", id],
  report: (id: string, period: string | null, pending: boolean) => [...queryRoots.budgets, "report", id, period, pending],
} as const;

export function listBudgets(): Promise<Budget[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/budgets", { params: { query: { limit: 500, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export const getBudget = (id: string) => unwrap(api.GET("/api/v1/budgets/{budget_id}", { params: { path: { budget_id: id } } }));

export const getBudgetReport = (id: string, period: string | null, includePending: boolean) =>
  unwrap(
    api.GET("/api/v1/budgets/{budget_id}/report", {
      params: {
        path: { budget_id: id },
        query: { ...(period === null ? {} : { period }), ...(includePending ? { include_pending: true } : {}) },
      },
    }),
  );

export const createBudget = (body: BudgetCreate) => unwrap(api.POST("/api/v1/budgets", { body }));

export const updateBudget = (id: string, body: BudgetUpdate) =>
  unwrap(api.PATCH("/api/v1/budgets/{budget_id}", { params: { path: { budget_id: id } }, body }));

export const deleteBudget = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/budgets/{budget_id}", { params: { path: { budget_id: id } } }));
};
