// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Where to go after logging in. `return_to` comes from the address bar, so it
 * is untrusted: only a same-origin, relative path is accepted (no scheme, no
 * host, no `//` or `/\` that a browser would read as another host, no
 * control characters, no `.` or `..` path segment, also not percent-encoded,
 * since the URL parser would resolve `/a/..//evil.example` to
 * `//evil.example`). Anything else becomes "/". The normalised result is
 * checked again, so it can never start with `//` or `/\`. Mirrors the
 * server's `return_to` rule for OIDC (docs/AUTH.md).
 */

export const DEFAULT_RETURN_TO = "/";
const MAX_LENGTH = 2048;
const BASE = "https://perfin.invalid";
// Pages that would only loop back into the login flow (the OIDC link page is
// a legitimate target: it is where a local login for linking returns to).
const LOOP_PATHS = new Set([
  "/login",
  "/login/mfa",
  "/login/enroll-mfa",
  "/setup",
  "/reset-password",
  "/forgot-password",
]);

function hasControlCharacters(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x20 || code === 0x7f) {
      return true;
    }
  }
  return false;
}

// A "." or ".." segment, plain or percent-encoded ("%2e", "%2E").
const DOT_SEGMENT = /(^|\/)(\.|%2e){1,2}(\/|$)/i;

function hasDotSegment(value: string): boolean {
  const end = value.search(/[?#]/);
  return DOT_SEGMENT.test(end === -1 ? value : value.slice(0, end));
}

/** Would a browser read this as another host ("//host" or "/\host")? */
function leavesTheOrigin(value: string): boolean {
  return value.startsWith("//") || value.startsWith("/\\");
}

/** A safe relative path (path, query and fragment) or {@link DEFAULT_RETURN_TO}. */
export function safeReturnTo(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_LENGTH) {
    return DEFAULT_RETURN_TO;
  }
  if (!value.startsWith("/") || leavesTheOrigin(value) || value.includes("\\") || hasControlCharacters(value)) {
    return DEFAULT_RETURN_TO;
  }
  if (hasDotSegment(value)) {
    return DEFAULT_RETURN_TO;
  }
  let url: URL;
  try {
    url = new URL(value, BASE);
  } catch {
    return DEFAULT_RETURN_TO;
  }
  if (url.origin !== BASE) {
    return DEFAULT_RETURN_TO;
  }
  const path = url.pathname;
  if (LOOP_PATHS.has(path.replace(/\/+$/, ""))) {
    return DEFAULT_RETURN_TO;
  }
  const result = `${path}${url.search}${url.hash}`;
  // Belt and braces: whatever the parser made of the input, never hand out
  // a protocol-relative target (review P1-SR2-02).
  return leavesTheOrigin(result) ? DEFAULT_RETURN_TO : result;
}

/** `path?return_to=<encoded target>`; the default target is left out. */
export function withReturnTo(path: string, target: string): string {
  const safe = safeReturnTo(target);
  return safe === DEFAULT_RETURN_TO ? path : `${path}?return_to=${encodeURIComponent(safe)}`;
}

/** The `return_to` query parameter of a raw search string ("?a=b"), made safe. */
export function returnToFromSearch(search: string): string {
  return safeReturnTo(new URLSearchParams(search).get("return_to"));
}
