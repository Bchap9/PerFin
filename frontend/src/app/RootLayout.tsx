// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useState } from "react";

import { cx } from "../components/cx";
import { AccountMenu } from "../features/auth/AccountMenu";
import { isBarePath } from "../features/auth/guard";
import { IconButton } from "../components/IconButton";
import { CloseIcon, MenuIcon } from "../components/icons";
import { sessionQueryOptions } from "../lib/session";
import { NAV_ITEMS } from "./nav";
import { LOGIN_PATH } from "./shellRoutes";
import { ThemeToggle } from "./ThemeProvider";
import { useLiveUpdates } from "./useLiveUpdates";

/** Paths rendered without the navigation chrome (sign-in, setup, reset; see features/auth). */
function isBare(pathname: string): boolean {
  return pathname === LOGIN_PATH || isBarePath(pathname);
}

function SessionIndicator() {
  const session = useQuery(sessionQueryOptions());
  useLiveUpdates(session.data?.status === "authenticated");
  return <AccountMenu />;
}

export function RootLayout() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const [navOpen, setNavOpen] = useState(false);
  const bare = isBare(pathname);

  return (
    <div className={cx("app-shell", bare && "app-shell--bare")}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {bare ? null : (
        <header className="app-header">
          <IconButton
            className="app-nav-toggle"
            label={navOpen ? "Close navigation" : "Open navigation"}
            icon={navOpen ? <CloseIcon /> : <MenuIcon />}
            aria-expanded={navOpen}
            aria-controls="app-sidebar"
            onClick={() => {
              setNavOpen((open) => !open);
            }}
          />
          <Link to="/" className="app-brand">
            PerFin
          </Link>
          <span className="app-header__spacer" />
          <SessionIndicator />
          <ThemeToggle />
        </header>
      )}
      {bare ? null : (
        <nav id="app-sidebar" className={cx("app-sidebar", navOpen && "app-sidebar--open")} aria-label="Main">
          <ul className="app-nav__list">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <Link
                  to={item.to}
                  className="app-nav__link"
                  activeOptions={{ exact: item.to === "/" }}
                  onClick={() => {
                    setNavOpen(false);
                  }}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      )}
      <main id="main" className="app-main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
