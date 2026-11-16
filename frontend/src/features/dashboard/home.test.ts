// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  comingUp,
  comparison,
  greeting,
  needsYouRows,
  needsYouSeriesRows,
  netWorthChange,
  spendingPeriods,
  topCategories,
} from "./home";

const money = (amount: string, currency = "USD") => ({ amount, currency });

describe("greeting", () => {
  const at = (hour: number, minute = 0) => new Date(2026, 10, 16, hour, minute);

  it("says morning from five until noon, afternoon until six, evening otherwise", () => {
    expect(greeting(at(4, 59))).toBe("Good evening");
    expect(greeting(at(5))).toBe("Good morning");
    expect(greeting(at(11, 59))).toBe("Good morning");
    expect(greeting(at(12))).toBe("Good afternoon");
    expect(greeting(at(17, 59))).toBe("Good afternoon");
    expect(greeting(at(18))).toBe("Good evening");
    expect(greeting(at(0))).toBe("Good evening");
  });
});

describe("spending periods", () => {
  it("compares this month so far with the same days of last month", () => {
    expect(spendingPeriods("2026-11-16")).toEqual({
      from: "2026-11-01",
      to: "2026-11-16",
      previousFrom: "2026-10-01",
      previousTo: "2026-10-16",
    });
  });

  it("stops at the end of a shorter month", () => {
    expect(spendingPeriods("2026-03-31")).toMatchObject({ previousFrom: "2026-02-01", previousTo: "2026-02-28" });
  });
});

describe("comparison", () => {
  it("says less, in percent, against the same point of last month", () => {
    expect(comparison("920.00", "1000.00", "October")).toEqual({ text: "8% less than this point in October", tone: "up" });
  });

  it("says more without calling it an error", () => {
    expect(comparison("1120.00", "1000.00", "October")).toEqual({ text: "12% more than this point in October", tone: "neutral" });
  });

  it("reads spending signs as magnitudes", () => {
    expect(comparison("-920.00", "-1000.00", "October").text).toBe("8% less than this point in October");
  });

  it("has a sentence for nothing spent last month and for the same amount", () => {
    expect(comparison("50.00", "0", "October")).toEqual({ text: "Nothing spent by this point in October", tone: "neutral" });
    expect(comparison("100.00", "100.00", "October")).toEqual({ text: "The same as this point in October", tone: "neutral" });
    expect(comparison("100.20", "100.00", "October").text).toBe("About the same as this point in October");
  });
});

describe("top categories", () => {
  const item = (key: string | null, name: string | null, amount: string) => ({ key, name, amount: money(amount) });

  it("keeps the five largest and adds the rest up as Other", () => {
    const rows = [
      item("a", "Groceries", "50.00"),
      item("b", "Rent", "900.00"),
      item(null, null, "40.00"),
      item("c", "Fuel", "60.00"),
      item("d", "Dining", "70.00"),
      item("e", "Phone", "30.00"),
      item("f", "Gifts", "20.00"),
    ];
    expect(topCategories(rows)).toEqual([
      { key: "b", label: "Rent", amount: "900.00" },
      { key: "d", label: "Dining", amount: "70.00" },
      { key: "c", label: "Fuel", amount: "60.00" },
      { key: "a", label: "Groceries", amount: "50.00" },
      { key: null, label: "Uncategorised", amount: "40.00" },
      { key: "other", label: "Other", amount: "50.00" },
    ]);
  });

  it("has no Other when five or fewer", () => {
    expect(topCategories([item("a", "Groceries", "-50.00")])).toEqual([{ key: "a", label: "Groceries", amount: "50.00" }]);
  });
});

describe("net worth change", () => {
  const point = (as_of: string, net: string) => ({ as_of, net: money(net) });

  it("is measured from the last point before the month began", () => {
    const points = [point("2026-09-30", "100.00"), point("2026-10-31", "150.00"), point("2026-11-16", "175.00")];
    expect(netWorthChange("200.00", points, "2026-11-16")).toBe("50.00");
  });

  it("is unknown without a point before the month", () => {
    expect(netWorthChange("200.00", [point("2026-11-16", "200.00")], "2026-11-16")).toBeNull();
  });
});

describe("coming up", () => {
  const subscription = (id: string, patch: Record<string, unknown> = {}) => ({
    id,
    name: `Series ${id}`,
    kind: "subscription" as const,
    status: "confirmed" as const,
    next_due: "2026-11-20",
    amount: money("-17.99"),
    amount_min: money("-17.99"),
    amount_max: money("-17.99"),
    price_change: null,
    ...patch,
  });
  const schedule = (id: string, patch: Record<string, unknown> = {}) => ({
    id,
    name: `Schedule ${id}`,
    kind: "bill" as const,
    status: "active" as const,
    active: true,
    next_due: "2026-11-25",
    amount_min: money("-1850.00"),
    amount_max: money("-1850.00"),
    ...patch,
  });

  it("merges both lists by date without counting a series twice", () => {
    const items = comingUp(
      [subscription("s-1", { next_due: "2026-11-30" })],
      [schedule("s-1", { next_due: "2026-11-30" }), schedule("r-1", { next_due: "2026-11-18" })],
      "2026-11-16",
    );
    expect(items.map((entry) => entry.id)).toEqual(["r-1", "s-1"]);
  });

  it("keeps the next 30 days, leaves out income and what is not running, and stops at five", () => {
    const items = comingUp(
      [
        subscription("late", { next_due: "2026-12-17" }),
        subscription("income", { kind: "income", amount: money("2100.00") }),
        subscription("maybe", { status: "suggested" }),
        subscription("gone", { status: "dismissed" }),
        subscription("past", { next_due: "2026-11-10" }),
      ],
      [
        schedule("paused", { status: "paused", active: false }),
        ...["a", "b", "c", "d", "e", "f"].map((id, index) => schedule(id, { next_due: `2026-11-2${String(index)}` })),
      ],
      "2026-11-16",
    );
    expect(items.map((entry) => entry.id)).toEqual(["a", "b", "c", "d", "e"]);
  });

  it("notes a price change and the amount it went up by", () => {
    const [item] = comingUp(
      [subscription("s-1", { price_change: { previous: money("-15.99"), current: money("-17.99"), on: "2026-10-12", pending: false } })],
      [],
      "2026-11-16",
    );
    expect(item).toMatchObject({ amount: "17.99", approximate: false, note: "went up $2.00 last month" });
  });

  it("shows an amount that varies as about the middle, with its usual range", () => {
    const [item] = comingUp([], [schedule("r-1", { amount_min: money("-105.00"), amount_max: money("-125.00") })], "2026-11-16");
    expect(item).toMatchObject({ amount: "115", approximate: true, note: "usually $105.00 to $125.00" });
  });
});

describe("needs you rows", () => {
  const card = (id: string, action: string, created_at: string) => ({
    action,
    document: { id, original_filename: `${id}.pdf`, created_at },
  });

  it("orders documents as the Inbox does, oldest first in a group, then loose items", () => {
    const rows = needsYouRows(
      [
        card("note", "acknowledge", "2026-11-01T00:00:00Z"),
        card("import-new", "choose_account", "2026-11-10T00:00:00Z"),
        card("money", "reconcile", "2026-11-12T00:00:00Z"),
        card("import-old", "map_columns", "2026-11-02T00:00:00Z"),
      ],
      [{ id: "loose-1", kind: "possible_duplicate" }],
    );
    expect(rows.map((row) => row.id)).toEqual(["money", "import-old", "import-new", "note", "loose-1"]);
    expect(rows[0]).toMatchObject({ title: "money.pdf", question: "Check the balance against the statement", document: "money" });
    expect(rows[4]).toMatchObject({ document: null });
  });

  it("puts an action it does not know with the notes", () => {
    const rows = needsYouRows([card("odd", "something_new", "2026-11-01T00:00:00Z"), card("ask", "teach", "2026-11-05T00:00:00Z")], []);
    expect(rows.map((row) => row.id)).toEqual(["ask", "odd"]);
  });
});

describe("needs you series rows", () => {
  const series = (id: string, name: string, occurrences: number, kind: "subscription" | "bill" = "subscription") => ({
    id,
    name,
    kind,
    occurrences,
  });
  const change = (
    id: string,
    schedule_id: string,
    name: string,
    kind: "price_changed" | "missed" | "new",
    occurred_on: string,
    before: string | null = null,
    after: string | null = null,
  ) => ({
    id,
    schedule_id,
    name,
    kind,
    occurred_on,
    amount_before: before === null ? null : money(before),
    amount_after: after === null ? null : money(after),
  });

  it("puts series with a change first, newest change first, then suggested series by most charges", () => {
    const rows = needsYouSeriesRows(
      [series("s-few", "Puzzle Club", 2), series("s-many", "Cloud Locker", 6, "bill"), series("s-old", "Paper Weekly", 4)],
      [
        change("e-1", "s-tv", "Streamly Plus", "price_changed", "2026-11-02", "-15.99", "-17.99"),
        change("e-2", "s-old", "Paper Weekly", "missed", "2026-11-09"),
        change("e-3", "s-tv", "Streamly Plus", "missed", "2026-10-01"),
      ],
    );
    expect(rows.map((row) => row.id)).toEqual(["series:s-old", "series:s-tv", "series:s-many", "series:s-few"]);
    expect(rows.map((row) => row.title)).toEqual(["Paper Weekly", "Streamly Plus", "Cloud Locker", "Puzzle Club"]);
    expect(rows[0]?.question).toBe("Expected on Nov 9, 2026, not seen");
    expect(rows[1]?.question).toBe("Price went from $15.99 to $17.99 on Nov 2, 2026, and 1 more change");
    expect(rows[2]?.question).toBe("Might be recurring");
    expect(rows[3]?.question).toBe("Might be a subscription");
    expect(rows.every((row) => row.document === null)).toBe(true);
  });
});
