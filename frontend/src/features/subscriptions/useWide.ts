// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Whether the viewport is at least as wide as the line where
 * `subscriptions.css` turns cards into a table (720px). One series opens as a
 * dialog over the list above the line and as a page of its own below it.
 */
import { useSyncExternalStore } from "react";

const WIDE_QUERY = "(min-width: 720px)";

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  const query = window.matchMedia(WIDE_QUERY);
  query.addEventListener("change", onChange);
  return () => {
    query.removeEventListener("change", onChange);
  };
}

/** Wide where the question cannot be asked (no `matchMedia`). */
function isWide(): boolean {
  return typeof window.matchMedia !== "function" || window.matchMedia(WIDE_QUERY).matches;
}

export function useWide(): boolean {
  return useSyncExternalStore(subscribe, isWide, () => true);
}
