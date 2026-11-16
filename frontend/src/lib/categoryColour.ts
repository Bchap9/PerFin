// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The colour of a category, as one of six chip classes (`chip--s1` …
 * `chip--s6`, drawn from the `--color-series-N` tokens). Every category takes
 * the colour of its top-level category, so a group reads as one colour.
 *
 * The default groups (their `system_key`, backend
 * `domain/transactions/categories.py`) have fixed classes:
 *
 * | group                         | class |
 * |-------------------------------|-------|
 * | food, income                  | s1    |
 * | transport, financial          | s2    |
 * | leisure                       | s3    |
 * | shopping                      | s4    |
 * | health, family                | s5    |
 * | housing, utilities, transfers, excluded | s6 |
 *
 * Any other top-level category gets a class from a stable FNV-1a hash of its
 * id. No category (`null`) is `chip--none`.
 */

export const GROUP_CLASSES: ReadonlyMap<string, string> = new Map([
  ["food", "chip--s1"],
  ["income", "chip--s1"],
  ["transport", "chip--s2"],
  ["financial", "chip--s2"],
  ["leisure", "chip--s3"],
  ["shopping", "chip--s4"],
  ["health", "chip--s5"],
  ["family", "chip--s5"],
  ["housing", "chip--s6"],
  ["utilities", "chip--s6"],
  ["transfers", "chip--s6"],
  ["excluded", "chip--s6"],
]);

export interface ColourCategory {
  id: string;
  parent_id: string | null;
  system_key: string | null;
}

const SERIES = 6;

/** 32-bit FNV-1a over the UTF-16 code units of `text`. */
export function fnv1a(text: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

function hashClass(id: string): string {
  return `chip--s${String((fnv1a(id) % SERIES) + 1)}`;
}

export function categoryColourClass(categoryId: string | null, tree: readonly ColourCategory[]): string {
  if (categoryId === null) {
    return "chip--none";
  }
  const byId = new Map(tree.map((category) => [category.id, category]));
  let current = byId.get(categoryId);
  if (current === undefined) {
    return hashClass(categoryId);
  }
  const seen = new Set<string>([current.id]);
  while (current.parent_id !== null) {
    const parent = byId.get(current.parent_id);
    if (parent === undefined || seen.has(parent.id)) {
      break;
    }
    seen.add(parent.id);
    current = parent;
  }
  const fixed = current.parent_id === null && current.system_key !== null ? GROUP_CLASSES.get(current.system_key) : undefined;
  return fixed ?? hashClass(current.id);
}
