// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { MappingEditorPage } from "./MappingEditor";
import { UploadPage } from "./UploadPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({
      getParentRoute: () => parent,
      path: "/documents",
      component: UploadPage,
    }),
    createRoute({
      getParentRoute: () => parent,
      path: "/documents/$documentId/mapping",
      component: MappingEditorPage,
    }),
  ];
}
