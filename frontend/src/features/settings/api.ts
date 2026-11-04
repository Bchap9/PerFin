// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for the settings pages: profile, tokens, household members. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type UserPreferences = Schemas["UserPreferencesOut"];
export type UserPreferencesUpdate = Schemas["UserPreferencesUpdate"];
export type Token = Schemas["TokenOut"];
export type TokenCreated = Schemas["TokenCreatedOut"];
export type ScopeCatalog = Schemas["ScopeCatalogOut"];
export type Member = Schemas["MemberOut"];
export type MemberAdd = Schemas["MemberAdd"];
export type HouseholdRole = Schemas["HouseholdRole"];

export const settingsKeys = {
  user: [...queryRoots.settings, "user"],
  sessions: [...queryRoots.settings, "sessions"],
  tokens: [...queryRoots.settings, "tokens"],
  scopes: [...queryRoots.settings, "token-scopes"],
  members: (householdId: string) => [...queryRoots.settings, "members", householdId],
} as const;

export function getUserPreferences(): Promise<UserPreferences> {
  return unwrap(api.GET("/api/v1/settings/user"));
}

export function updateUserPreferences(body: UserPreferencesUpdate): Promise<UserPreferences> {
  return unwrap(api.PATCH("/api/v1/settings/user", { body }));
}

export function listTokens(): Promise<Token[]> {
  return unwrap(api.GET("/api/v1/auth/tokens"));
}

export function getScopeCatalog(): Promise<ScopeCatalog> {
  return unwrap(api.GET("/api/v1/auth/tokens/scopes"));
}

export function createToken(body: Schemas["TokenCreate"]): Promise<TokenCreated> {
  return unwrap(api.POST("/api/v1/auth/tokens", { body }));
}

export async function revokeToken(tokenId: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/auth/tokens/{token_id}", { params: { path: { token_id: tokenId } } }));
}

export function listMembers(householdId: string): Promise<Member[]> {
  return unwrap(api.GET("/api/v1/households/{household_id}/members", { params: { path: { household_id: householdId } } }));
}

export function addMember(householdId: string, body: MemberAdd): Promise<Member> {
  return unwrap(
    api.POST("/api/v1/households/{household_id}/members", { params: { path: { household_id: householdId } }, body }),
  );
}

export function changeMemberRole(householdId: string, userId: string, role: HouseholdRole): Promise<Member> {
  return unwrap(
    api.PATCH("/api/v1/households/{household_id}/members/{user_id}", {
      params: { path: { household_id: householdId, user_id: userId } },
      body: { role },
    }),
  );
}

export async function removeMember(householdId: string, userId: string): Promise<void> {
  await unwrap(
    api.DELETE("/api/v1/households/{household_id}/members/{user_id}", {
      params: { path: { household_id: householdId, user_id: userId } },
    }),
  );
}

// ---- receipts mailbox (intake) -------------------------------------------------------

export type MailIntake = Schemas["MailIntakeOut"];
export type MailIntakeUpdate = Schemas["MailIntakeUpdate"];
export type MailTest = Schemas["MailTestOut"];

export const intakeKey = [...queryRoots.settings, "intake"] as const;

export function getIntake(): Promise<MailIntake> {
  return unwrap(api.GET("/api/v1/settings/intake"));
}

export function patchIntake(body: MailIntakeUpdate): Promise<MailIntake> {
  return unwrap(api.PATCH("/api/v1/settings/intake", { body }));
}

export function testIntake(): Promise<MailTest> {
  return unwrap(api.POST("/api/v1/settings/intake/email/test"));
}

export function rotateAlias(): Promise<Schemas["AliasOut"]> {
  return unwrap(api.POST("/api/v1/settings/intake/email/rotate-alias"));
}
