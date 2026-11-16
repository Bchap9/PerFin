// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The shell: a rail of the six sections (Home, Inbox, Transactions, Accounts,
 * Plan, Reports), then Settings, the account menu and the theme toggle at its
 * foot; the AI banner and the page in `main`.
 *
 * Every link exists once. The widths are CSS only (layout.css):
 * - 1024 px and wider: the rail with labels;
 * - 820 to 1023 px: an icon rail, labels visually hidden but still the names;
 * - under 820 px: the rail's brand and foot become a top bar and its section
 *   links a bottom bar; Settings is reached from the account menu.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";

import { cx } from "../components/cx";
import { GearIcon } from "../components/icons";
import { AccountMenu } from "../features/auth/AccountMenu";
import { isBarePath } from "../features/auth/guard";
import { AiBanner } from "../features/settings/ai/AiBanner";
import { sessionQueryOptions } from "../lib/session";
import { CommandPalette } from "./CommandPalette";
import { InboxBadge } from "./InboxBadge";
import { SECTIONS, sectionForPath } from "./routeMap";
import { ThemeToggle } from "./ThemeProvider";
import { useLiveUpdates } from "./useLiveUpdates";

function SessionIndicator() {
  const session = useQuery(sessionQueryOptions());
  useLiveUpdates(session.data?.status === "authenticated");
  return <AccountMenu />;
}

function Rail({ pathname }: { pathname: string }) {
  const current = sectionForPath(pathname);
  const inSettings = pathname === "/settings" || pathname.startsWith("/settings/");
  return (
    <div className="app-rail">
      <Link to="/" className="app-brand" activeProps={{}}>
        Per<span className="app-brand__accent">Fin</span>
      </Link>
      <nav className="app-nav" aria-label="Sections">
        <ul className="app-nav__list">
          {SECTIONS.map((section) => {
            const Icon = section.icon;
            return (
              <li key={section.id} className="app-nav__item">
                <Link
                  to={section.to}
                  className="app-nav__link"
                  aria-current={current === section.id ? "page" : undefined}
                  activeOptions={{ exact: true, includeSearch: true }}
                  activeProps={{}}
                >
                  <Icon className="app-nav__icon" />
                  <span className="app-nav__label">{section.label}</span>
                  {section.id === "inbox" ? <InboxBadge /> : null}
                </Link>
              </li>
            );
          })}
          <li className="app-nav__item app-nav__item--settings">
            <Link
              to="/settings"
              className="app-nav__link"
              aria-current={inSettings ? "page" : undefined}
              activeOptions={{ exact: true, includeSearch: true }}
              activeProps={{}}
            >
              <GearIcon className="app-nav__icon" />
              <span className="app-nav__label">Settings</span>
            </Link>
          </li>
        </ul>
      </nav>
      <div className="app-rail__foot">
        <SessionIndicator />
        <ThemeToggle />
      </div>
    </div>
  );
}

export function RootLayout() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const bare = isBarePath(pathname);

  return (
    <div className={cx("app-shell", bare && "app-shell--bare")}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {bare ? null : <Rail pathname={pathname} />}
      <main id="main" className="app-main" tabIndex={-1}>
        <div className="app-main__inner">
          {bare ? null : <AiBanner />}
          <Outlet />
        </div>
      </main>
      {bare ? null : <CommandPalette />}
    </div>
  );
}
