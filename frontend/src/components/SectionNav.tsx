// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The tab bar of a section: a named `nav` of links (not ARIA tabs, since each
 * item is its own address). Its items come from `app/routeMap.ts`.
 *
 * Exactly one item is current (`aria-current="page"`), or none when the
 * address is outside the section:
 * - the item whose `to` is the longest prefix of the path, on a segment
 *   boundary, wins (`/plan/subscriptions/s-1` marks Subscriptions);
 * - among items with that same `to`, the one whose `search` values all match
 *   the address wins, else the one without `search` (a default tab is left
 *   out of the address).
 *
 * An item may keep named search parameters (`keepSearch`) when followed, so
 * the period of a report survives a switch of view. A `count` shows in a badge
 * and is part of the link's name ("Needs you, 3").
 */
import { Link, useRouterState, useSearch } from "@tanstack/react-router";

import { cx } from "./cx";

export interface SectionNavItem {
  to: string;
  label: string;
  search?: Readonly<Record<string, string>>;
  keepSearch?: readonly string[];
  count?: number;
}

export interface SectionNavProps {
  label: string;
  items: readonly SectionNavItem[];
}

function onPath(pathname: string, to: string): boolean {
  if (to === "/") {
    return pathname === "/";
  }
  const base = to.endsWith("/") ? to.slice(0, -1) : to;
  return pathname === base || pathname.startsWith(`${base}/`);
}

function asText(value: unknown): string | undefined {
  if (typeof value === "string") {
    return value;
  }
  return typeof value === "number" || typeof value === "boolean" ? String(value) : undefined;
}

/** Index of the current item, or -1. */
export function currentIndex(
  items: readonly SectionNavItem[],
  pathname: string,
  search: Readonly<Record<string, unknown>>,
): number {
  const values = new Map(Object.entries(search));
  let bestLength = -1;
  for (const item of items) {
    if (onPath(pathname, item.to) && item.to.length > bestLength) {
      bestLength = item.to.length;
    }
  }
  if (bestLength < 0) {
    return -1;
  }
  const candidates = items
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => onPath(pathname, item.to) && item.to.length === bestLength);
  const bySearch = candidates.find(
    ({ item }) =>
      item.search !== undefined && Object.entries(item.search).every(([key, value]) => asText(values.get(key)) === value),
  );
  if (bySearch !== undefined) {
    return bySearch.index;
  }
  return candidates.find(({ item }) => item.search === undefined)?.index ?? -1;
}

function keptSearch(item: SectionNavItem, previous: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const prev = new Map(Object.entries(previous));
  const kept = new Map<string, unknown>();
  for (const key of item.keepSearch ?? []) {
    if (prev.has(key)) {
      kept.set(key, prev.get(key));
    }
  }
  for (const [key, value] of Object.entries(item.search ?? {})) {
    kept.set(key, value);
  }
  return Object.fromEntries(kept);
}

export function SectionNav({ label, items }: SectionNavProps) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const search: Record<string, unknown> = useSearch({ strict: false });
  const current = currentIndex(items, pathname, search);
  return (
    <nav aria-label={label} className="section-nav">
      <ul className="section-nav__list">
        {items.map((item, index) => (
          <li key={`${item.to}?${new URLSearchParams(item.search).toString()}`}>
            <Link
              to={item.to}
              search={keptSearch(item, search)}
              className={cx("section-nav__link", index === current && "section-nav__link--current")}
              aria-current={index === current ? "page" : undefined}
              activeOptions={{ exact: true, includeSearch: true }}
              activeProps={{}}
            >
              <span className="section-nav__label">{item.label}</span>
              {item.count === undefined ? null : (
                <>
                  <span className="visually-hidden">{`, ${String(item.count)}`}</span>
                  <span className="section-nav__count" aria-hidden="true">
                    {item.count}
                  </span>
                </>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
