// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { DocumentDetailPage } from "./DocumentDetailPage";
import { DocumentsPage } from "./DocumentsPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({ getParentRoute: () => parent, path: "/documents/library", component: DocumentsPage }),
    createRoute({ getParentRoute: () => parent, path: "/documents/$documentId", component: DocumentDetailPage }),
  ];
}
