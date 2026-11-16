// SPDX-License-Identifier: AGPL-3.0-or-later
import { NEW_ROUTES, OLD_ROUTES, REDIRECTS, SECTIONS, SETTINGS_GROUPS, sectionForPath } from "./routeMap";

const newRoutes = new Set(NEW_ROUTES);

describe("route map", () => {
  it("every old route has a home", () => {
    expect(new Set(OLD_ROUTES).size).toBe(OLD_ROUTES.length);
    expect(OLD_ROUTES).toHaveLength(49);
    for (const path of OLD_ROUTES) {
      const redirects = REDIRECTS.filter((row) => row.from === path);
      expect({ path, home: newRoutes.has(path) || redirects.length > 0 }).toEqual({ path, home: true });
    }
  });

  it("every old route is either a new route or the from of exactly one redirect", () => {
    expect(REDIRECTS).toHaveLength(24);
    for (const path of OLD_ROUTES) {
      const count = REDIRECTS.filter((row) => row.from === path).length;
      expect({ path, count }).toEqual({ path, count: newRoutes.has(path) ? 0 : 1 });
    }
    // A redirect starts at an old address and never at a page that still exists.
    for (const row of REDIRECTS) {
      expect(OLD_ROUTES).toContain(row.from);
      expect(newRoutes.has(row.from)).toBe(false);
    }
  });

  it("no redirect target is itself redirected", () => {
    const sources = new Set(REDIRECTS.map((row) => row.from));
    for (const row of REDIRECTS) {
      expect({ from: row.from, to: row.to, redirected: sources.has(row.to) }).toEqual({ from: row.from, to: row.to, redirected: false });
      expect(newRoutes.has(row.to)).toBe(true);
    }
  });

  it("every section tab points at a new route", () => {
    expect(SECTIONS.map((section) => section.label)).toEqual(["Home", "Inbox", "Transactions", "Accounts", "Plan", "Reports"]);
    for (const section of SECTIONS) {
      expect(newRoutes.has(section.to)).toBe(true);
      for (const tab of section.tabs) {
        expect({ section: section.id, tab: tab.label, routed: newRoutes.has(tab.to) }).toEqual({
          section: section.id,
          tab: tab.label,
          routed: true,
        });
      }
    }
    for (const group of SETTINGS_GROUPS) {
      for (const page of group.pages) {
        expect(newRoutes.has(page.to)).toBe(true);
      }
    }
  });

  it("finds the section of an address", () => {
    expect(sectionForPath("/")).toBe("home");
    expect(sectionForPath("/inbox/documents/d-1")).toBe("inbox");
    expect(sectionForPath("/transactions")).toBe("transactions");
    expect(sectionForPath("/accounts/investments")).toBe("accounts");
    expect(sectionForPath("/plan/budgets/b-1")).toBe("plan");
    expect(sectionForPath("/reports/tax/2025")).toBe("reports");
    expect(sectionForPath("/settings/profile")).toBeNull();
    expect(sectionForPath("/planner")).toBeNull();
  });
});
