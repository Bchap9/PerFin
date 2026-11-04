// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Goal = Schemas["GoalOut"];
export type GoalCreate = Schemas["GoalCreate"];
export type GoalUpdate = Schemas["GoalUpdate"];
export type Contribution = Schemas["ContributionOut"];

export const goalKeys = {
  list: (archived: boolean) => [...queryRoots.goals, "list", archived],
  contributions: (id: string) => [...queryRoots.goals, "contributions", id],
} as const;

export function listGoals(archived: boolean): Promise<Goal[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/goals", { params: { query: { limit: 500, archived, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export const createGoal = (body: GoalCreate) => unwrap(api.POST("/api/v1/goals", { body }));

export const updateGoal = (id: string, body: GoalUpdate) =>
  unwrap(api.PATCH("/api/v1/goals/{goal_id}", { params: { path: { goal_id: id } }, body }));

export const deleteGoal = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/goals/{goal_id}", { params: { path: { goal_id: id } } }));
};

export function listContributions(id: string): Promise<Contribution[]> {
  return collectPages((cursor) =>
    unwrap(
      api.GET("/api/v1/goals/{goal_id}/contributions", {
        params: { path: { goal_id: id }, query: { limit: 500, ...(cursor === null ? {} : { cursor }) } },
      }),
    ),
  );
}

export const addContribution = (id: string, body: Schemas["ContributionCreate"]) =>
  unwrap(api.POST("/api/v1/goals/{goal_id}/contributions", { params: { path: { goal_id: id } }, body }));

export const removeContribution = async (id: string, contributionId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/goals/{goal_id}/contributions/{contribution_id}", {
      params: { path: { goal_id: id, contribution_id: contributionId } },
    }),
  );
};
