// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Category } from "../planning/queries";

export interface CategoryNode {
  category: Category;
  depth: number;
}

/**
 * The categories as a depth-first list (parents before their children,
 * siblings by name). A category whose parent is missing is shown at the top.
 * Cycles cannot be built by the server; a guard stops one anyway.
 */
export function flattenCategories(categories: readonly Category[]): CategoryNode[] {
  const ids = new Set(categories.map((category) => category.id));
  const children = new Map<string | null, Category[]>();
  for (const category of categories) {
    const parent = category.parent_id !== null && ids.has(category.parent_id) ? category.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), category]);
  }
  const result: CategoryNode[] = [];
  const seen = new Set<string>();
  const visit = (parent: string | null, depth: number): void => {
    const level = [...(children.get(parent) ?? [])].sort((a, b) => a.name.localeCompare(b.name));
    for (const category of level) {
      if (seen.has(category.id)) {
        continue;
      }
      seen.add(category.id);
      result.push({ category, depth });
      visit(category.id, depth + 1);
    }
  };
  visit(null, 0);
  return result;
}

/** Ids of a category and everything below it (a category cannot move under itself). */
export function descendantIds(categories: readonly Category[], id: string): Set<string> {
  const found = new Set<string>([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const category of categories) {
      if (category.parent_id !== null && found.has(category.parent_id) && !found.has(category.id)) {
        found.add(category.id);
        grew = true;
      }
    }
  }
  return found;
}

/** "Food > Groceries" style labels for pickers. */
export function categoryLabels(categories: readonly Category[]): Map<string, string> {
  const byId = new Map(categories.map((category) => [category.id, category]));
  const labels = new Map<string, string>();
  for (const category of categories) {
    const names = [category.name];
    let parent = category.parent_id === null ? undefined : byId.get(category.parent_id);
    for (let hops = 0; parent !== undefined && hops < 10; hops += 1) {
      names.unshift(parent.name);
      parent = parent.parent_id === null ? undefined : byId.get(parent.parent_id);
    }
    labels.set(category.id, names.join(" > "));
  }
  return labels;
}

/** Picker options sorted by hierarchy, labelled with the full path. */
export function categoryOptions(
  categories: readonly Category[],
  filter: (category: Category) => boolean = () => true,
): { value: string; label: string }[] {
  const labels = categoryLabels(categories);
  return flattenCategories(categories)
    .filter((node) => filter(node.category))
    .map((node) => ({ value: node.category.id, label: labels.get(node.category.id) ?? node.category.name }));
}
