// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { ReviewPage } from "./ReviewPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({
      getParentRoute: () => parent,
      path: "/review",
      component: ReviewPage,
    }),
  ];
}
