// SPDX-License-Identifier: AGPL-3.0-or-later
import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";

import { authGuard } from "../features/auth/guard";
import { AppProviders } from "./AppProviders";
import { NotFoundPage, PendingPage, RouteErrorPage } from "./pages";
import { RootLayout } from "./RootLayout";
import { buildRouteTree } from "./routeTree";

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

export function createAppRouter(context: RouterContext, history?: RouterHistory) {
  return createRouter({
    routeTree: rootRoute.addChildren(buildRouteTree(rootRoute)),
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
