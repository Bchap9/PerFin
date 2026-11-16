// SPDX-License-Identifier: AGPL-3.0-or-later
// Stage 3 bridge: lane S replaces this.
import { useSearch } from "@tanstack/react-router";

import { CategoryMapPage } from "../tax/CategoryMapPage";
import { CategoriesPage } from "./CategoriesPage";

/** Settings, Categories (`/settings/categories`): today's categories page, or the tax hints for `?tab=tax`. */
export function CategoriesSettingsPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  return search["tab"] === "tax" ? <CategoryMapPage /> : <CategoriesPage />;
}
