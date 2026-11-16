// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Where a detail page sits: a `nav` named "Breadcrumbs" with an ordered list
 * of links. The last item is the page itself, plain text with
 * `aria-current="page"`. A `to` may carry a query (`/accounts?group=cash`).
 */
import { Link } from "@tanstack/react-router";

export interface Crumb {
  to?: string;
  label: string;
}

function splitTo(to: string): { path: string; search: Record<string, string> } {
  const at = to.indexOf("?");
  if (at < 0) {
    return { path: to, search: {} };
  }
  return { path: to.slice(0, at), search: Object.fromEntries(new URLSearchParams(to.slice(at + 1))) };
}

export function Breadcrumbs({ items }: { items: readonly Crumb[] }) {
  return (
    <nav aria-label="Breadcrumbs" className="breadcrumbs">
      <ol className="breadcrumbs__list">
        {items.map((item, index) => {
          const last = index === items.length - 1;
          if (last || item.to === undefined) {
            return (
              <li key={`${String(index)}-${item.label}`} className="breadcrumbs__item">
                <span aria-current={last ? "page" : undefined}>{item.label}</span>
              </li>
            );
          }
          const { path, search } = splitTo(item.to);
          return (
            <li key={`${String(index)}-${item.label}`} className="breadcrumbs__item">
              <Link to={path} search={search} activeProps={{}} activeOptions={{ exact: true, includeSearch: true }}>
                {item.label}
              </Link>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
