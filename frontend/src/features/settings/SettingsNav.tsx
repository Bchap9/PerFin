// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Settings side list: the groups of `routeMap.SETTINGS_GROUPS`, each a
 * heading and its links, with the current page marked `aria-current`. It
 * shows from 1024 px (settings.css); below that the `/settings` index is the
 * list, reached from each page's breadcrumbs. "Users" is listed only for an
 * instance administrator (the page itself checks again).
 */
import { Link, useRouterState } from "@tanstack/react-router";

import { SETTINGS_GROUPS, type SettingsPageLink } from "../../app/routeMap";
import { useAuthSession } from "../auth/session";

export interface SettingsGroup {
  label: string;
  pages: readonly SettingsPageLink[];
}

/** The groups this person sees: instance-only pages, and a group left empty by them, are dropped. */
export function visibleSettingsGroups(isInstanceAdmin: boolean): SettingsGroup[] {
  return SETTINGS_GROUPS.map((group) => ({
    label: group.label,
    pages: group.pages.filter((page) => page.instanceAdmin !== true || isInstanceAdmin),
  })).filter((group) => group.pages.length > 0);
}

export function useIsInstanceAdmin(): boolean {
  const { session } = useAuthSession();
  return session?.user.is_instance_admin === true;
}

/** A page is current on its own path and on the pages below it (Rules for `/settings/rules/new`). */
function isCurrent(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}

function slug(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

export function SettingsNav() {
  const groups = visibleSettingsGroups(useIsInstanceAdmin());
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  return (
    <nav aria-label="Settings" className="settings-nav">
      {groups.map((group) => {
        const headingId = `settings-nav-${slug(group.label)}`;
        return (
          <div key={group.label} className="settings-nav__group">
            <h2 id={headingId} className="settings-nav__heading">
              {group.label}
            </h2>
            <ul className="settings-nav__list" aria-labelledby={headingId}>
              {group.pages.map((page) => {
                const current = isCurrent(pathname, page.to);
                return (
                  <li key={page.to}>
                    <Link
                      to={page.to}
                      className="settings-nav__link"
                      aria-current={current ? "page" : undefined}
                      activeProps={{}}
                    >
                      {page.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}
