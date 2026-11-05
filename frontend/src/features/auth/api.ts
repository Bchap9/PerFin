// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Typed calls for the identity endpoints (docs/API.md "Auth"). Each returns
 * the response data or throws an `ApiError` (lib/api/client.ts); the CSRF
 * header is added by the client's middleware.
 */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";

type Schemas = components["schemas"];

export type LoginOptions = Schemas["LoginOptionsOut"];
export type LoginResult = Schemas["LoginOut"];
export type MfaVerified = Schemas["MfaVerifiedOut"];
export type MfaStatus = Schemas["MfaStatusOut"];
export type MfaMethod = Schemas["MfaMethodOut"];
export type FactorAdded = Schemas["FactorAddedOut"];
export type TotpEnrollment = Schemas["TotpEnrollmentOut"];
export type SessionInfo = Schemas["SessionInfoOut"];
export type Household = Schemas["HouseholdOut"];
export type HouseholdSwitched = Schemas["HouseholdSwitchOut"];
export type LinkOffer = Schemas["LinkOfferOut"];

/** One second-factor proof for `POST /auth/mfa/verify` and `POST /auth/step-up`. */
export type MfaProof =
  | { method: "totp"; code: string }
  | { method: "recovery"; code: string }
  | { method: "webauthn"; credential: Record<string, unknown> };

export const OIDC_START_PATH = "/api/v1/auth/oidc/start";

export function getLoginOptions(): Promise<LoginOptions> {
  return unwrap(api.GET("/api/v1/auth/login/options"));
}

export async function getOidcEnabled(): Promise<boolean> {
  return (await unwrap(api.GET("/api/v1/auth/oidc/status"))).enabled;
}

export function getSetupRequired(): Promise<boolean> {
  return unwrap(api.GET("/api/v1/setup/status")).then((status) => status.setup_required);
}

export function login(body: Schemas["LoginIn"]): Promise<LoginResult> {
  return unwrap(api.POST("/api/v1/auth/login", { body }));
}

export async function logout(): Promise<void> {
  await unwrap(api.POST("/api/v1/auth/logout"));
}

/** RP-initiated OIDC logout: the IdP's end-session URL, or `null`. */
export async function oidcLogout(): Promise<string | null> {
  return (await unwrap(api.POST("/api/v1/auth/oidc/logout", { body: {} }))).redirect_url;
}

export function verifyMfa(proof: MfaProof): Promise<MfaVerified> {
  return unwrap(api.POST("/api/v1/auth/mfa/verify", { body: proof }));
}

export function stepUp(proof: MfaProof): Promise<MfaVerified> {
  return unwrap(api.POST("/api/v1/auth/step-up", { body: proof }));
}

export function webauthnAuthenticationOptions(): Promise<Record<string, unknown>> {
  return unwrap(api.POST("/api/v1/auth/mfa/webauthn/authentication-options"));
}

export function getMfaStatus(): Promise<MfaStatus> {
  return unwrap(api.GET("/api/v1/auth/mfa"));
}

export function startTotpEnrollment(): Promise<TotpEnrollment> {
  return unwrap(api.POST("/api/v1/auth/mfa/totp"));
}

export function confirmTotpEnrollment(code: string, name: string): Promise<FactorAdded> {
  return unwrap(api.POST("/api/v1/auth/mfa/totp/confirm", { body: { code, name } }));
}

export async function removeTotp(): Promise<void> {
  await unwrap(api.DELETE("/api/v1/auth/mfa/totp"));
}

export function webauthnRegistrationOptions(): Promise<Record<string, unknown>> {
  return unwrap(api.POST("/api/v1/auth/mfa/webauthn/registration-options"));
}

export function registerWebauthn(credential: Record<string, unknown>, name: string): Promise<FactorAdded> {
  return unwrap(api.POST("/api/v1/auth/mfa/webauthn", { body: { credential, name } }));
}

export async function removeWebauthn(methodId: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/auth/mfa/webauthn/{method_id}", { params: { path: { method_id: methodId } } }));
}

export async function regenerateRecoveryCodes(): Promise<string[]> {
  return (await unwrap(api.POST("/api/v1/auth/mfa/recovery-codes"))).codes;
}

export function changePassword(body: Schemas["PasswordChangeIn"]): Promise<Schemas["PasswordChangedOut"]> {
  return unwrap(api.POST("/api/v1/auth/password", { body }));
}

export async function requestPasswordReset(identifier: string): Promise<void> {
  await unwrap(api.POST("/api/v1/auth/password-reset/request", { body: { identifier } }));
}

export async function confirmPasswordReset(token: string, newPassword: string): Promise<void> {
  await unwrap(api.POST("/api/v1/auth/password-reset/confirm", { body: { token, new_password: newPassword } }));
}

export function listSessions(): Promise<SessionInfo[]> {
  return unwrap(api.GET("/api/v1/auth/sessions"));
}

export async function revokeSession(sessionId: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/auth/sessions/{session_id}", { params: { path: { session_id: sessionId } } }));
}

export async function revokeOtherSessions(): Promise<number> {
  return (await unwrap(api.DELETE("/api/v1/auth/sessions"))).revoked;
}

export function listHouseholds(): Promise<Household[]> {
  return unwrap(api.GET("/api/v1/households"));
}

export function switchHousehold(householdId: string): Promise<HouseholdSwitched> {
  return unwrap(api.POST("/api/v1/auth/session/household", { body: { household_id: householdId } }));
}

export function getLinkOffer(): Promise<LinkOffer> {
  return unwrap(api.GET("/api/v1/auth/oidc/link"));
}

export async function confirmLink(): Promise<void> {
  await unwrap(api.POST("/api/v1/auth/oidc/link"));
}

export async function discardLink(): Promise<void> {
  await unwrap(api.DELETE("/api/v1/auth/oidc/link"));
}

/**
 * The browser navigation that starts an OIDC login. `reauth` asks the
 * identity provider to authenticate again even with an SSO session
 * (`prompt=login`): the step-up path for accounts that rely on the
 * provider's MFA, which only counts while the provider's own
 * authentication is recent (docs/AUTH.md, A6).
 */
export function oidcStartHref(returnTo: string, options: { reauth?: boolean } = {}): string {
  const params = new URLSearchParams();
  if (returnTo !== "/") {
    params.set("return_to", returnTo);
  }
  if (options.reauth === true) {
    params.set("reauth", "true");
  }
  const query = params.toString();
  return query === "" ? OIDC_START_PATH : `${OIDC_START_PATH}?${query}`;
}
