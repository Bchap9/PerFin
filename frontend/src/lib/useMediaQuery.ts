// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Whether a media query matches, following its changes. Safe where
 * `matchMedia` does not exist (it answers false). The layout's widths are
 * constants here and literals in CSS, because a media query cannot read a
 * CSS variable:
 * - `PHONE_MAX` (819px and below): top bar, bottom bar, drawer as a sheet;
 * - `TABLE_CARDS_MAX` (719px and below): tables switch to cards;
 * - `ICON_RAIL_MAX` (1023px and below, above the phone): the icon rail.
 */
import { useCallback, useSyncExternalStore } from "react";

export const PHONE_MAX = 819;
export const TABLE_CARDS_MAX = 719;
export const ICON_RAIL_MAX = 1023;

function mediaList(query: string): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return null;
  }
  return window.matchMedia(query);
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = mediaList(query);
      if (list === null) {
        return () => undefined;
      }
      list.addEventListener("change", onChange);
      return () => {
        list.removeEventListener("change", onChange);
      };
    },
    [query],
  );
  const read = useCallback(() => mediaList(query)?.matches ?? false, [query]);
  return useSyncExternalStore(subscribe, read, () => false);
}
