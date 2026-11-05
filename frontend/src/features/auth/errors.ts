// SPDX-License-Identifier: AGPL-3.0-or-later
/** User-facing messages for the auth problems (docs/API.md). */
import { errorMessage, isApiError } from "../../lib/api/client";
import { isWebAuthnCeremonyError } from "./webauthn";

const MESSAGES = new Map<string, string>(Object.entries({
  "invalid-credentials": "The username or password is not correct.",
  "rate-limited": "Too many attempts. Wait a few minutes and try again.",
  "local-login-disabled": "Password sign-in is turned off here. Use single sign-on.",
  "mfa-invalid": "That code or key was not accepted. Try again.",
  "no-webauthn-credentials": "No security key is registered. Use another method.",
  "invalid-reset-token": "This reset link is invalid, expired or already used. Ask for a new one.",
  "password-reset-unavailable": "Password reset by email is not available on this server. Ask your administrator.",
  "invalid-setup-token": "The setup link is wrong or has expired. Run `perfin bootstrap-url` on the server for a new one.",
  "step-up-required": "Verify your second factor to continue.",
  "session-required": "This needs a signed-in browser session.",
}));

const WEAK_PASSWORD = new Map<string, string>(Object.entries({
  "too-short": "Use at least 12 characters.",
  "too-long": "Use at most 128 characters.",
  breached: "This password appears in known data breaches. Choose another one.",
  "same-as-username": "The password must not be your username.",
  "same-as-current": "Choose a password you have not used here.",
}));

/** A short message for an error, with the auth-specific wording where there is one. */
export function authErrorMessage(error: unknown): string {
  if (isWebAuthnCeremonyError(error)) {
    return error.message;
  }
  if (!isApiError(error)) {
    return errorMessage(error);
  }
  const slug = error.slug;
  if (slug === "weak-password") {
    const reason = error.extensions["reason"];
    const known = typeof reason === "string" ? WEAK_PASSWORD.get(reason) : undefined;
    return known ?? error.detail ?? "Choose a stronger password.";
  }
  const known = slug === null ? undefined : MESSAGES.get(slug);
  if (known !== undefined) {
    return known;
  }
  if (error.status === 429) {
    return MESSAGES.get("rate-limited") ?? errorMessage(error);
  }
  return errorMessage(error);
}

/** Validation messages keyed by field name, for forms. */
export function fieldErrorsOf(error: unknown): Record<string, string> {
  return isApiError(error) ? error.fieldErrors() : {};
}

/** Client-side password rules that mirror the server's (12-128 characters). */
export function passwordProblem(chosen: string, repeated: string): string | null {
  if (chosen.length < 12) {
    return "Use at least 12 characters.";
  }
  if (chosen.length > 128) {
    return "Use at most 128 characters.";
  }
  if (chosen !== repeated) {
    return "The passwords do not match.";
  }
  return null;
}
