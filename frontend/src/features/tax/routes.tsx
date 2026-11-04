// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { CategoryMapPage } from "./CategoryMapPage";
import { TaxYearPage } from "./TaxYearPage";
import { TaxYearsPage } from "./TaxYearsPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({ getParentRoute: () => parent, path: "/tax", component: TaxYearsPage }),
    createRoute({ getParentRoute: () => parent, path: "/tax/mapping", component: CategoryMapPage }),
    createRoute({ getParentRoute: () => parent, path: "/tax/$year", component: TaxYearPage }),
  ];
}
