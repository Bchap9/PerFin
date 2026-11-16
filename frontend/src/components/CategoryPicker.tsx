// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef } from "react";

import { Select, type SelectProps } from "./Select";

/** What the picker needs to know about a category. */
export interface PickerCategory {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface CategoryPickerProps extends Omit<SelectProps, "options" | "children"> {
  categories: readonly PickerCategory[];
}

interface Group {
  top: PickerCategory;
  below: { category: PickerCategory; label: string }[];
}

const byName = (a: PickerCategory, b: PickerCategory): number => a.name.localeCompare(b.name);

/**
 * The categories as top-level entries, each with everything under it (depth
 * first, siblings by name). A category whose parent is not in the list counts
 * as top-level. Below the second level the label carries the path.
 */
function groupCategories(categories: readonly PickerCategory[]): Group[] {
  const ids = new Set(categories.map((category) => category.id));
  const children = new Map<string | null, PickerCategory[]>();
  for (const category of categories) {
    const parent = category.parent_id !== null && ids.has(category.parent_id) ? category.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), category]);
  }
  const seen = new Set<string>();
  const collect = (parent: string, prefix: string, out: Group["below"]): void => {
    for (const category of [...(children.get(parent) ?? [])].sort(byName)) {
      if (seen.has(category.id)) {
        continue;
      }
      seen.add(category.id);
      const label = `${prefix}${category.name}`;
      out.push({ category, label });
      collect(category.id, `${label} > `, out);
    }
  };
  return [...(children.get(null) ?? [])].sort(byName).map((top) => {
    seen.add(top.id);
    const below: Group["below"] = [];
    collect(top.id, "", below);
    return { top, below };
  });
}

/**
 * A native select of categories: one `<optgroup>` per top-level category that
 * has categories under it (the group's own category comes first), and a first,
 * empty option, "Uncategorised" unless `placeholder` says otherwise. It takes
 * the props of `Select`.
 */
export const CategoryPicker = forwardRef<HTMLSelectElement, CategoryPickerProps>(function CategoryPicker(
  { categories, placeholder = "Uncategorised", ...rest },
  ref,
) {
  return (
    <Select ref={ref} placeholder={placeholder} {...rest}>
      {groupCategories(categories).map(({ top, below }) =>
        below.length === 0 ? (
          <option key={top.id} value={top.id}>
            {top.name}
          </option>
        ) : (
          <optgroup key={top.id} label={top.name}>
            <option value={top.id}>{top.name}</option>
            {below.map(({ category, label }) => (
              <option key={category.id} value={category.id}>
                {label}
              </option>
            ))}
          </optgroup>
        ),
      )}
    </Select>
  );
});
