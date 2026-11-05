// SPDX-License-Identifier: AGPL-3.0-or-later
/** Writes for categories, payees and tags (the reads live in features/planning/queries.ts). */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";

type Schemas = components["schemas"];

export type CategoryCreate = Schemas["CategoryCreate"];
export type CategoryUpdate = Schemas["CategoryUpdate"];
export type PayeeCreate = Schemas["PayeeCreate"];
export type PayeeUpdate = Schemas["PayeeUpdate"];

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
