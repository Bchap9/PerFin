// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { SubscriptionDetail } from "./SubscriptionDetail";
import { SubscriptionsPage } from "./SubscriptionsPage";

/** The list, and one series as its child so the list stays mounted behind the dialog. */
export function routes(parent: AnyRoute): AnyRoute[] {
  const list = createRoute({ getParentRoute: () => parent, path: "/subscriptions", component: SubscriptionsPage });
  const detail = createRoute({ getParentRoute: () => list, path: "$id", component: SubscriptionDetail });
  return [list.addChildren([detail])];
}
