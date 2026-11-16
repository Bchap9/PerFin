// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The rail's items: the six sections of `routeMap.ts` (the single source for
 * the rail, the tab bars, the palette and the redirects). Kept as a thin
 * re-export for older imports.
 */
import { SECTIONS, type Section } from "./routeMap";

export type NavItem = Section;

export const NAV_ITEMS: readonly NavItem[] = SECTIONS;
