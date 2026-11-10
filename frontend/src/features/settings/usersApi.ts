// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Instance administration of users (`/admin/users`, docs/AUTH.md). Only an
 * instance administrator on a browser session may call these, every change
 * needs a recent second-factor check (step-up), and nobody changes their own
 * account here (`409 own-account`).
 */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type AdminUser = Schemas["AdminUserOut"];
export type AdminUserCreate = Schemas["AdminUserCreate"];
export type AdminUserUpdate = Schemas["AdminUserUpdate"];

export const userKeys = {
  all: [...queryRoots.settings, "users"],
  list: [...queryRoots.settings, "users", "list"],
  one: (id: string) => [...queryRoots.settings, "users", "one", id],
} as const;

/** Every user of the instance, in the order they were created. */
export function listUsers(): Promise<AdminUser[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/admin/users", { params: { query: { limit: 200, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function getUser(userId: string): Promise<AdminUser> {
  return unwrap(api.GET("/api/v1/admin/users/{user_id}", { params: { path: { user_id: userId } } }));
}

/** A local account with a password, in no household; never an instance admin or break-glass account. */
export function createUser(body: AdminUserCreate): Promise<AdminUser> {
  return unwrap(api.POST("/api/v1/admin/users", { body }));
}

/** `is_active`, `is_breakglass` and `email_verified` are all this can change. */
export function updateUser(userId: string, body: AdminUserUpdate): Promise<AdminUser> {
  return unwrap(api.PATCH("/api/v1/admin/users/{user_id}", { params: { path: { user_id: userId } }, body }));
}

/** Remove every second factor and recovery code of the user and end their sessions. */
export function resetUserMfa(userId: string): Promise<Schemas["MfaResetOut"]> {
  return unwrap(api.POST("/api/v1/admin/users/{user_id}/mfa-reset", { params: { path: { user_id: userId } } }));
}
