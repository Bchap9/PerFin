// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * `Idempotency-Key` for creating `POST`s (docs/API.md "Conventions").
 *
 * The server runs the first request with a key and replays its answer to a
 * repeat with the same key and body; the same key with another body is `422
 * idempotency-key-reused`, and only successful answers are stored. So one
 * key belongs to one thing the person asked for: a double click or a retry
 * after a lost answer sends the key again, a changed form gets a new one,
 * and after a success the key is dropped so that asking for the same thing
 * again later really runs again.
 */
import { useMemo, useRef } from "react";

/** A fresh key: a random UUID (visible ASCII, well under the 255 character limit). */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Pages served without TLS have no randomUUID; getRandomValues is always there.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export interface IdempotencyKeys {
  /** The key for the request described by `signature`; the same signature gets the same key until `done()`. */
  keyFor: (signature: string) => string;
  /** Forget the key after the request succeeded. */
  done: () => void;
}

export function useIdempotencyKey(): IdempotencyKeys {
  const current = useRef<{ signature: string; key: string } | null>(null);
  return useMemo(
    () => ({
      keyFor(signature: string): string {
        if (current.current?.signature !== signature) {
          current.current = { signature, key: newIdempotencyKey() };
        }
        return current.current.key;
      },
      done(): void {
        current.current = null;
      },
    }),
    [],
  );
}
