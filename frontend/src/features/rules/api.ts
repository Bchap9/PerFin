// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import type { RuleDoc } from "./builder";

type Schemas = components["schemas"];

export type Rule = Schemas["RuleOut"];
export type RuleTestBody = Schemas["RuleTest"];
export type RuleTestResult = Schemas["RuleTestResult"];
export type RuleTestItem = Schemas["RuleTestItem"];
export type Job = Schemas["JobOut"];

export const ruleKeys = {
  list: [...queryRoots.rules, "list"],
  one: (id: string) => [...queryRoots.rules, "one", id],
  job: (id: string) => [...queryRoots.jobs, id],
} as const;

export async function listRules(): Promise<Rule[]> {
  return (await unwrap(api.GET("/api/v1/rules"))).items;
}

export const getRule = (id: string) => unwrap(api.GET("/api/v1/rules/{rule_id}", { params: { path: { rule_id: id } } }));

export const createRule = (body: RuleDoc) => unwrap(api.POST("/api/v1/rules", { body }));

export const updateRule = (id: string, body: Schemas["RuleUpdate"]) =>
  unwrap(api.PATCH("/api/v1/rules/{rule_id}", { params: { path: { rule_id: id } }, body }));

export const deleteRule = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/rules/{rule_id}", { params: { path: { rule_id: id } } }));
};

/** Every rule id, once, in the new order. */
export const reorderRules = (ruleIds: string[]) => unwrap(api.POST("/api/v1/rules/reorder", { body: { rule_ids: ruleIds } }));

export const testRule = (body: RuleTestBody) => unwrap(api.POST("/api/v1/rules/test", { body }));

export const applyRules = (body: Schemas["RuleApply"]) => unwrap(api.POST("/api/v1/rules/apply", { body }));

export const getJob = (id: string) => unwrap(api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } }));

/** `ids` moved one place: the full id order after moving `id` up (-1) or down (1). */
export function movedOrder(ids: readonly string[], id: string, direction: -1 | 1): string[] | null {
  const from = ids.indexOf(id);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= ids.length) {
    return null;
  }
  const next = [...ids];
  const [moved] = next.splice(from, 1);
  if (moved === undefined) {
    return null;
  }
  next.splice(to, 0, moved);
  return next;
}
