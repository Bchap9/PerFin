// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

import { cx } from "../components/cx";
import { AccountMenu } from "../features/auth/AccountMenu";
import { AiBanner } from "../features/settings/ai/AiBanner";
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
  // The drawer is open for one path: navigating elsewhere closes it.
  const [openAt, setOpenAt] = useState<string | null>(null);
  const navOpen = openAt === pathname;
  const setNavOpen = useCallback(
    (open: boolean | ((value: boolean) => boolean)): void => {
      setOpenAt((current) => ((typeof open === "function" ? open(current === pathname) : open) ? pathname : null));
    },
    [pathname],
  );
  const bare = isBare(pathname);

  // The small-screen drawer closes on Escape.
  useEffect(() => {
    if (!navOpen) {
      return undefined;
    }
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        setNavOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [navOpen, setNavOpen]);

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
      {bare || !navOpen ? null : (
        <div
          className="app-nav-backdrop"
          aria-hidden="true"
          onClick={() => {
            setNavOpen(false);
          }}
        />
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
        {bare ? null : <AiBanner />}
        <Outlet />
      </main>
    </div>
  );
}
