// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Page state kept in the address, so a view can be bookmarked and the Back
 * button returns to it. Both hooks write with `replace` (switching a tab is
 * not a new history entry), leave the default out of the address, and keep
 * every other search parameter as it was. The router parses search values
 * as JSON, so `?q=2024` arrives as a number; it is read back as text.
 * Lifted from `features/reports/searchState.ts`.
 */
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";

function asText(value: unknown): string | undefined {
  if (typeof value === "string") {
    return value;
  }
  return typeof value === "number" || typeof value === "boolean" ? String(value) : undefined;
}

/** The search object with `name` set to `value`, or without it when `value` is the fallback. */
export function withSearchParam(
  previous: Readonly<Record<string, unknown>>,
  name: string,
  value: string,
  fallback: string,
): Record<string, unknown> {
  const next = new Map(Object.entries(previous));
  if (value === fallback) {
    next.delete(name);
  } else {
    next.set(name, value);
  }
  return Object.fromEntries(next);
}

/** One search parameter as text, with a setter. */
export function useSearchParam(name: string, fallback: string): [string, (value: string) => void] {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const navigate = useNavigate();
  const value = asText(new Map(Object.entries(search)).get(name)) ?? fallback;
  const set = useCallback(
    (next: string): void => {
      void navigate({
        to: ".",
        search: (previous: Record<string, unknown>) => withSearchParam(previous, name, next, fallback),
        replace: true,
      });
    },
    [navigate, name, fallback],
  );
  return [value, set];
}

/** A tab id from the address; an unknown value reads as `fallback`, and an unknown id is never written. */
export function useTabParam<T extends string>(
  ids: readonly T[],
  fallback: T,
  name = "tab",
): [T, (id: T) => void] {
  const [raw, setRaw] = useSearchParam(name, fallback);
  const value = ids.find((id) => id === raw) ?? fallback;
  const set = useCallback(
    (id: T): void => {
      setRaw(ids.includes(id) ? id : fallback);
    },
    [ids, fallback, setRaw],
  );
  return [value, set];
}
