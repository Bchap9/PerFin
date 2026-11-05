// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One-time secrets in links (the setup token `#t=`, the password reset token
 * `#token=`) travel in the URL fragment, which browsers never send to a
 * server or put in a `Referer` (docs/AUTH.md). These helpers read them from
 * the fragment, or from the query string as a fallback for links that a mail
 * client rewrote.
 */

const MAX_SECRET_LENGTH = 256;
const SECRET_RE = /^[A-Za-z0-9_\-.~]+$/;

function clean(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.length <= MAX_SECRET_LENGTH && SECRET_RE.test(trimmed) ? trimmed : null;
}

/** A parameter from a fragment ("t=abc" or "#t=abc"). */
export function fragmentParam(hash: string, name: string): string | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  return clean(new URLSearchParams(raw).get(name));
}

/** The first of `names` found in the fragment, then in the query string. */
export function linkSecret(hash: string, search: string, names: readonly string[]): string | null {
  for (const name of names) {
    const value = fragmentParam(hash, name);
    if (value !== null) {
      return value;
    }
  }
  const query = new URLSearchParams(search);
  for (const name of names) {
    const value = clean(query.get(name));
    if (value !== null) {
      return value;
    }
  }
  return null;
}
