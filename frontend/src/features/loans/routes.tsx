// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { LoanDetailPage } from "./LoanDetailPage";
import { LoansPage } from "./LoansPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({ getParentRoute: () => parent, path: "/loans", component: LoansPage }),
    createRoute({ getParentRoute: () => parent, path: "/loans/$loanId", component: LoanDetailPage }),
  ];
}
