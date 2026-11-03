// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { HouseholdMembersPage } from "./HouseholdMembersPage";
import { ProfilePage } from "./ProfilePage";
import { SecurityPage } from "./SecurityPage";
import { SettingsIndexPage, SettingsLayout } from "./SettingsLayout";

export function routes(parent: AnyRoute): AnyRoute[] {
  const layout = createRoute({ getParentRoute: () => parent, path: "/settings", component: SettingsLayout });
  const child = (path: string, component: () => React.JSX.Element) =>
    createRoute({ getParentRoute: () => layout, path, component });
  return [
    layout.addChildren([
      child("/", SettingsIndexPage),
      child("profile", ProfilePage),
      child("security", SecurityPage),
      child("household", HouseholdMembersPage),
    ]),
  ];
}
