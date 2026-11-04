// SPDX-License-Identifier: AGPL-3.0-or-later
/** Full export and import (docs/PORTABILITY.md), household settings and account grants. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type ExportJob = Schemas["ExportOut"];
export type ImportJob = Schemas["ImportOut"];
export type ArchiveState = Schemas["ArchiveState"];
export type Household = Schemas["HouseholdOut"];
export type GrantLevel = Schemas["GrantLevel"];
export type Grant = Schemas["AccountGrantOut"];

export const dataKeys = {
  export: (id: string) => [...queryRoots.settings, "export", id],
  import: (id: string) => [...queryRoots.settings, "import", id],
  grants: (householdId: string, userId: string) => [...queryRoots.settings, "grants", householdId, userId],
} as const;

export const startExport = (passphrase: string | null) =>
  unwrap(api.POST("/api/v1/export", { body: passphrase === null ? {} : { passphrase } }));

export const getExport = (id: string) => unwrap(api.GET("/api/v1/export/{job_id}", { params: { path: { job_id: id } } }));

/** The archive's bytes. Works once; needs a recent step-up. */
export async function downloadExport(id: string): Promise<Blob> {
  const data: unknown = await unwrap(
    api.GET("/api/v1/export/{job_id}/download", { params: { path: { job_id: id } }, parseAs: "blob" }),
  );
  return data as Blob;
}

export const startImport = (file: File, passphrase: string | null) =>
  unwrap(
    api.POST("/api/v1/import", {
      body: { file: "" },
      bodySerializer: () => {
        const form = new FormData();
        form.append("file", file);
        return form;
      },
      params: { header: passphrase === null ? {} : { "X-Archive-Passphrase": passphrase } },
    }),
  );

export const getImport = (id: string) => unwrap(api.GET("/api/v1/import/{job_id}", { params: { path: { job_id: id } } }));

export const updateHousehold = (id: string, body: Schemas["HouseholdUpdate"]) =>
  unwrap(api.PATCH("/api/v1/households/{household_id}", { params: { path: { household_id: id } }, body }));

export const listMemberGrants = (householdId: string, userId: string) =>
  unwrap(
    api.GET("/api/v1/households/{household_id}/members/{user_id}/grants", {
      params: { path: { household_id: householdId, user_id: userId } },
    }),
  );

export const setMemberGrant = (householdId: string, userId: string, accountId: string, level: GrantLevel) =>
  unwrap(
    api.PUT("/api/v1/households/{household_id}/members/{user_id}/grants/{account_id}", {
      params: { path: { household_id: householdId, user_id: userId, account_id: accountId } },
      body: { level },
    }),
  );

export const revokeMemberGrant = async (householdId: string, userId: string, accountId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/households/{household_id}/members/{user_id}/grants/{account_id}", {
      params: { path: { household_id: householdId, user_id: userId, account_id: accountId } },
    }),
  );
};
