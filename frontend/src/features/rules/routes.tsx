// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { EditRulePage, NewRulePage } from "./RuleEditorPage";
import { RulesPage } from "./RulesPage";

export function routes(parent: AnyRoute): AnyRoute[] {
  return [
    createRoute({ getParentRoute: () => parent, path: "/rules", component: RulesPage }),
    createRoute({ getParentRoute: () => parent, path: "/rules/new", component: NewRulePage }),
    createRoute({ getParentRoute: () => parent, path: "/rules/$ruleId", component: EditRulePage }),
  ];
}
