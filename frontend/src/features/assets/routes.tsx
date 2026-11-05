// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { AssetDetailPage } from "./AssetDetailPage";
import { AssetsPage } from "./AssetsPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({ getParentRoute: () => parent, path: "/assets", component: AssetsPage }),
    createRoute({ getParentRoute: () => parent, path: "/assets/$assetId", component: AssetDetailPage }),
  ];
}
