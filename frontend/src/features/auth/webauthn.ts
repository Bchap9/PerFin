// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * WebAuthn ceremonies through @simplewebauthn/browser. The server issues the
 * options (challenge, RP id from PERFIN_PUBLIC_URL) and checks the result;
 * this module only passes them between the server and the authenticator.
 */
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";

export class WebAuthnCeremonyError extends Error {
  constructor(message: string, options?: { cause: unknown }) {
    super(message, options);
    this.name = "WebAuthnCeremonyError";
  }
}

export function webauthnSupported(): boolean {
  try {
    return browserSupportsWebAuthn();
  } catch {
    return false;
  }
}

function hasChallenge(options: Record<string, unknown>): boolean {
  return typeof options["challenge"] === "string" && options["challenge"].length > 0;
}

function ceremonyError(cause: unknown): WebAuthnCeremonyError {
  const name = cause instanceof Error ? cause.name : "";
  const message =
    name === "NotAllowedError" || name === "AbortError"
      ? "The security key request was cancelled or timed out."
      : name === "InvalidStateError"
        ? "This security key is already registered."
        : "The security key could not be used. Try again or use another method.";
  return new WebAuthnCeremonyError(message, { cause });
}

/** Ask the authenticator to sign the server's challenge. */
export async function signChallenge(options: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (!hasChallenge(options)) {
    throw new WebAuthnCeremonyError("The server sent an invalid security key challenge.");
  }
  try {
    const response = await startAuthentication({
      optionsJSON: options as unknown as PublicKeyCredentialRequestOptionsJSON,
    });
    return { ...response };
  } catch (cause) {
    throw ceremonyError(cause);
  }
}

/** Ask the authenticator to create a credential for the server's options. */
export async function createCredential(options: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (!hasChallenge(options)) {
    throw new WebAuthnCeremonyError("The server sent an invalid security key challenge.");
  }
  try {
    const response = await startRegistration({
      optionsJSON: options as unknown as PublicKeyCredentialCreationOptionsJSON,
    });
    return { ...response };
  } catch (cause) {
    throw ceremonyError(cause);
  }
}

export function isWebAuthnCeremonyError(error: unknown): error is WebAuthnCeremonyError {
  return error instanceof WebAuthnCeremonyError;
}
