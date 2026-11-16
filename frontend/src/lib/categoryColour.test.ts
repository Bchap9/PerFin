// SPDX-License-Identifier: AGPL-3.0-or-later
import { categoryColourClass, GROUP_CLASSES } from "./categoryColour";

const TREE = [
  { id: "c-food", parent_id: null, system_key: "food" },
  { id: "c-groceries", parent_id: "c-food", system_key: "food.groceries" },
  { id: "c-bakery", parent_id: "c-groceries", system_key: null },
  { id: "c-transport", parent_id: null, system_key: "transport" },
  { id: "c-fuel", parent_id: "c-transport", system_key: "transport.fuel" },
  { id: "c-housing", parent_id: null, system_key: "housing" },
  { id: "c-utilities", parent_id: null, system_key: "utilities" },
  { id: "c-own", parent_id: null, system_key: null },
  { id: "c-own-child", parent_id: "c-own", system_key: null },
  { id: "c-orphan", parent_id: "c-missing", system_key: null },
  { id: "c-loop-a", parent_id: "c-loop-b", system_key: null },
  { id: "c-loop-b", parent_id: "c-loop-a", system_key: null },
];

describe("categoryColourClass", () => {
  it("a leaf resolves to its group's class", () => {
    expect(categoryColourClass("c-food", TREE)).toBe("chip--s1");
    expect(categoryColourClass("c-groceries", TREE)).toBe("chip--s1");
    expect(categoryColourClass("c-bakery", TREE)).toBe("chip--s1");
    expect(categoryColourClass("c-fuel", TREE)).toBe("chip--s2");
    expect(categoryColourClass("c-housing", TREE)).toBe("chip--s6");
    expect(categoryColourClass("c-utilities", TREE)).toBe("chip--s6");
  });

  it("names a class for every default group", () => {
    const groups = ["income", "housing", "utilities", "food", "transport", "health", "shopping", "leisure", "family", "financial", "transfers", "excluded"];
    for (const group of groups) {
      expect(GROUP_CLASSES.get(group)).toMatch(/^chip--s[1-6]$/);
    }
    expect(GROUP_CLASSES.get("leisure")).toBe("chip--s3");
    expect(GROUP_CLASSES.get("shopping")).toBe("chip--s4");
  });

  it("the hash is stable and in range", () => {
    const own = categoryColourClass("c-own", TREE);
    expect(own).toMatch(/^chip--s[1-6]$/);
    expect(categoryColourClass("c-own", TREE)).toBe(own);
    expect(categoryColourClass("c-own-child", TREE)).toBe(own);
    const many = new Set<string>();
    for (let index = 0; index < 200; index += 1) {
      const id = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
      const value = categoryColourClass(id, [{ id, parent_id: null, system_key: null }]);
      expect(value).toMatch(/^chip--s[1-6]$/);
      many.add(value);
    }
    // A hash that spreads: all six classes are used across 200 ids.
    expect(many.size).toBe(6);
  });

  it("a missing parent or a loop stops at the last category reached", () => {
    expect(categoryColourClass("c-orphan", TREE)).toMatch(/^chip--s[1-6]$/);
    expect(categoryColourClass("c-loop-a", TREE)).toMatch(/^chip--s[1-6]$/);
    expect(categoryColourClass("not-in-tree", TREE)).toMatch(/^chip--s[1-6]$/);
  });

  it("null gives chip--none", () => {
    expect(categoryColourClass(null, TREE)).toBe("chip--none");
  });
});
