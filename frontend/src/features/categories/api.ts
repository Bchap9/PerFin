// SPDX-License-Identifier: AGPL-3.0-or-later
/** Writes for categories, payees and tags (the reads live in features/planning/queries.ts). */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";

type Schemas = components["schemas"];

export type CategoryCreate = Schemas["CategoryCreate"];
export type CategoryUpdate = Schemas["CategoryUpdate"];
export type PayeeCreate = Schemas["PayeeCreate"];
export type PayeeUpdate = Schemas["PayeeUpdate"];
export type CategorisationSummary = Schemas["CategorisationSummary"];
export type SourceCount = Schemas["SourceCount"];
export type LearnedCategory = Schemas["LearnedCategory"];

/** How the transactions the caller may see got their categories, with the counts still open. */
export const getCategorisationSummary = () => unwrap(api.GET("/api/v1/categorisation/summary"));

const LEARNED_PAGE = 200;
/** A household keeps at most 5,000 learned rows; this is the stop for a server that never ends its pages. */
const LEARNED_PAGES_MAX = 30;

/**
 * What the household's own category choices taught PerFin, one entry per
 * merchant and direction: every page, in the server's order.
 */
export async function listLearned(): Promise<LearnedCategory[]> {
  const items: LearnedCategory[] = [];
  let cursor: string | null = null;
  for (let pages = 0; pages < LEARNED_PAGES_MAX; pages += 1) {
    const page: Schemas["LearnedCategoryPage"] = await unwrap(
      api.GET("/api/v1/categorisation/learned", {
        params: { query: { limit: LEARNED_PAGE, ...(cursor === null ? {} : { cursor }) } },
      }),
    );
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor === null) {
      break;
    }
  }
  return items;
}

/** Forget what was learned about one merchant. Transactions keep their categories. */
export const forgetLearned = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/categorisation/learned/{memory_id}", { params: { path: { memory_id: id } } }));
};

/** Look again at every transaction nobody decided, as a job (owners and admins). */
export const refreshCategorisation = () => unwrap(api.POST("/api/v1/categorisation/refresh"));

export const createCategory = (body: CategoryCreate) => unwrap(api.POST("/api/v1/categories", { body }));

export const updateCategory = (id: string, body: CategoryUpdate) =>
  unwrap(api.PATCH("/api/v1/categories/{category_id}", { params: { path: { category_id: id } }, body }));

export const deleteCategory = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/categories/{category_id}", { params: { path: { category_id: id } } }));
};

export const mergeCategory = (id: string, into: string) =>
  unwrap(api.POST("/api/v1/categories/{category_id}/merge", { params: { path: { category_id: id } }, body: { into } }));

export const createPayee = (body: PayeeCreate) => unwrap(api.POST("/api/v1/payees", { body }));

export const updatePayee = (id: string, body: PayeeUpdate) =>
  unwrap(api.PATCH("/api/v1/payees/{payee_id}", { params: { path: { payee_id: id } }, body }));

export const deletePayee = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/payees/{payee_id}", { params: { path: { payee_id: id } } }));
};

export const mergePayee = (id: string, into: string) =>
  unwrap(api.POST("/api/v1/payees/{payee_id}/merge", { params: { path: { payee_id: id } }, body: { into } }));

export const createTag = (name: string) => unwrap(api.POST("/api/v1/tags", { body: { name } }));

export const renameTag = (id: string, name: string) =>
  unwrap(api.PATCH("/api/v1/tags/{tag_id}", { params: { path: { tag_id: id } }, body: { name } }));

export const deleteTag = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/tags/{tag_id}", { params: { path: { tag_id: id } } }));
};
