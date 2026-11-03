// SPDX-License-Identifier: AGPL-3.0-or-later
import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRouter,
  type AnyRoute,
  type RouterHistory,
} from "@tanstack/react-router";

import { routes as authRoutes } from "../features/auth/routes";
import { authGuard } from "../features/auth/guard";
import { routes as homeRoutes } from "../features/home/routes";
import { routes as settingsRoutes } from "../features/settings/routes";
import { routes as setupRoutes } from "../features/setup/routes";
import { AppProviders } from "./AppProviders";
import { NotFoundPage, PendingPage, RouteErrorPage } from "./pages";
import { RootLayout } from "./RootLayout";
import { normalizedPath, shellRoutes } from "./shellRoutes";

export interface RouterContext {
  queryClient: QueryClient;
}

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  // Every page but sign-in, setup and password reset needs a session
  // (features/auth/guard.ts); the server enforces it again.
  beforeLoad: authGuard,
  component: RootLayout,
  notFoundComponent: NotFoundPage,
  errorComponent: RouteErrorPage,
});

/**
 * Feature route factories. Each feature's `routes.tsx` exports
 * `routes(parent)` returning its route subtree. Append new features at the
 * end of this list, one line each (docs/DEVELOPMENT.md, "Plug-in points").
 */
type FeatureRoutes = (parent: typeof rootRoute) => AnyRoute[];

const features: FeatureRoutes[] = [
  homeRoutes,
  authRoutes,
  setupRoutes,
  settingsRoutes,
];

/**
 * Feature routes plus the shell's placeholder routes for any navigation
 * section (and the login page) that no feature has registered yet.
 */
export function buildRouteTree() {
  const featureRoutes = features.flatMap((feature) => feature(rootRoute));
  const claimed = new Set(featureRoutes.map(normalizedPath));
  const fallbacks = shellRoutes(rootRoute).filter((route) => !claimed.has(normalizedPath(route)));
  return rootRoute.addChildren([...featureRoutes, ...fallbacks]);
}

export function createAppRouter(context: RouterContext, history?: RouterHistory) {
  return createRouter({
    routeTree: buildRouteTree(),
    context,
    defaultPreload: "intent",
    defaultErrorComponent: RouteErrorPage,
    defaultNotFoundComponent: NotFoundPage,
    defaultPendingComponent: PendingPage,
    // Providers live inside the router so a bare <RouterProvider> (tests,
    // main.tsx) always has the query client, theme and toasts.
    Wrap: ({ children }) => <AppProviders queryClient={context.queryClient}>{children}</AppProviders>,
    ...(history === undefined ? {} : { history }),
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
