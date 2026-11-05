// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Routes the shell serves until a feature claims the path: a placeholder
 * page per navigation section and the login placeholder. A feature that
 * registers the same path replaces the shell's route (see buildRouteTree).
 */
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { NAV_ITEMS } from "./nav";
import { LoginPage, PlaceholderPage } from "./pages";

export const LOGIN_PATH = "/login";

export function shellRoutes(parent: AnyRoute): AnyRoute[] {
  const placeholders = NAV_ITEMS.map((item) =>
    createRoute({
      getParentRoute: () => parent,
      path: item.to,
      component: function Placeholder() {
        return <PlaceholderPage title={item.label} summary={item.summary} />;
      },
    }),
  );
  const login = createRoute({
    getParentRoute: () => parent,
    path: LOGIN_PATH,
    component: LoginPage,
  });
  return [...placeholders, login];
}

/** A route's path normalised for comparison ("/accounts/" and "accounts" match). */
export function normalizedPath(route: AnyRoute): string | null {
  const options: unknown = route.options;
  if (typeof options !== "object" || options === null || !("path" in options)) {
    return null;
  }
  const { path } = options;
  return typeof path === "string" ? path.replace(/^\/+|\/+$/g, "") : null;
}
