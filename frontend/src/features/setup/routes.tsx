// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { SETUP_PATH } from "../auth/guard";
import { SetupPage } from "./SetupPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({
      getParentRoute: () => parent,
      path: SETUP_PATH,
      component: SetupPage,
    }),
  ];
}
