// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { CategoriesPage } from "../categories/CategoriesPage";
import { AccessPage } from "./AccessPage";
import { AiAccessPage } from "./ai/AiAccessPage";
import { DataPage } from "./DataPage";
import { GeneralPage } from "./GeneralPage";
import { IntakePage } from "./IntakePage";
import { NotificationsPage } from "./notifications/NotificationsPage";
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
      child("general", GeneralPage),
      child("access", AccessPage),
      child("categories", CategoriesPage),
      child("intake", IntakePage),
      child("ai", AiAccessPage),
      child("data", DataPage),
      child("notifications", NotificationsPage),
    ]),
  ];
}
