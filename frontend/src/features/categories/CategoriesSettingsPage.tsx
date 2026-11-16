// SPDX-License-Identifier: AGPL-3.0-or-later
import { Tabs } from "../../components/Tabs";
import { useTabParam } from "../../lib/urlState";
import { SettingsPage } from "../settings/SettingsLayout";
import { CategoryMapPage } from "../tax/CategoryMapPage";
import { CategoriesPanel, PayeesPanel, TagsPanel } from "./CategoriesPage";

const TABS = ["categories", "payees", "tags", "tax"] as const;
type Tab = (typeof TABS)[number];

/**
 * Settings, Categories (`/settings/categories`): the categories, payees, tags
 * and each category's tax hint, one tab each, the tab in `?tab=` so a link
 * (the old `/tax/mapping`, a tax year's "Edit tax hints") opens the right one.
 */
export function CategoriesSettingsPage() {
  const [tab, setTab] = useTabParam<Tab>(TABS, "categories");
  return (
    <SettingsPage title="Categories">
      <Tabs
        label="Categories, payees, tags and tax hints"
        className="categories-tabs"
        selectedId={tab}
        onSelect={(id) => {
          const next = TABS.find((candidate) => candidate === id);
          if (next !== undefined) {
            setTab(next);
          }
        }}
        tabs={[
          { id: "categories", label: "Categories", content: <CategoriesPanel /> },
          { id: "payees", label: "Payees", content: <PayeesPanel /> },
          { id: "tags", label: "Tags", content: <TagsPanel /> },
          { id: "tax", label: "Tax hints", content: <CategoryMapPage /> },
        ]}
      />
    </SettingsPage>
  );
}
