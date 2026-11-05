// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { AccountDetailPage } from "./AccountDetailPage";
import { AccountsPage } from "./AccountsPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({
      getParentRoute: () => parent,
      path: "/accounts",
      component: AccountsPage,
    }),
    createRoute({
      getParentRoute: () => parent,
      path: "/accounts/$accountId",
      component: AccountDetailPage,
    }),
  ];
}
