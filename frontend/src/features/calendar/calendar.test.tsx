// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ACCOUNT_CHECKING, money, ownerSession, referenceHandlers } from "../planning/testing";
import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, renderApp } from "../auth/testing";

const SCHEDULE = {
  id: "s-1",
  account_id: ACCOUNT_CHECKING,
  name: "Streaming",
  cadence: "monthly",
  interval_count: 1,
  next_due: "2026-11-10",
  amount_min: money("-15.99"),
  amount_max: money("-15.99"),
  payee_id: null,
  category_id: null,
  origin: "detected",
  active: true,
  last_seen_on: "2026-10-10",
  kind: "subscription",
  status: "active",
  status_source: "detector",
  merchant: "Streaming",
};

const schedule = (id: string, name: string, extra: Record<string, unknown> = {}) => ({ ...SCHEDULE, id, name, merchant: name, ...extra });

/** Thirty days with one bill due. */
const CALENDAR = {
  currency: "USD",
  date_from: "2026-11-03",
  date_to: "2026-12-03",
  opening_balance: money("1200.50"),
  income: money("0"),
  bills: money("-15.99"),
  net: money("-15.99"),
  unconverted: [],
  days: [
    { on: "2026-11-04", events: [], income: money("0"), bills: money("0"), net: money("0"), balance: money("1200.50") },
    {
      on: "2026-11-10",
      events: [{ schedule_id: "s-1", name: "Streaming", account_id: ACCOUNT_CHECKING, kind: "bill", amount: money("-15.99"), amount_min: money("-15.99"), amount_max: money("-15.99"), amount_base: null }],
      income: money("0"),
      bills: money("-15.99"),
      net: money("-15.99"),
      balance: money("1184.51"),
    },
  ],
};

describe("calendar", () => {
  it("shows what is due with the expected balance", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
    });
    renderApp("/plan/upcoming");
    const table = await screen.findByRole("table", { name: "Upcoming days" });
    // The page keeps its h2 for screen readers; the Plan tab already shows
    // its name, so it is not repeated as a visible heading.
    expect(screen.getByRole("heading", { level: 2, name: "Upcoming" })).toHaveClass("visually-hidden");
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getByText("Streaming")).toBeInTheDocument();
    expect(within(table).getByText("$1,184.51")).toBeInTheDocument();
  });

  it("the look-ahead is a compact control on the tab row, for the calendar only", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
    });
    renderApp("/plan/upcoming");
    await screen.findByRole("table", { name: "Upcoming days" });
    const user = userEvent.setup();
    const ahead = screen.getByRole("combobox", { name: "Look ahead" });
    // On the tab row, outside the panel, so it is not as wide as the content.
    expect(ahead.closest(".tabs__aside")).not.toBeNull();
    expect(within(screen.getByRole("tabpanel")).queryByRole("combobox", { name: "Look ahead" })).not.toBeInTheDocument();
    expect(within(ahead).getByRole("option", { name: "Next 90 days" })).toBeInTheDocument();
    await user.selectOptions(ahead, "90");
    await vi.waitFor(() => {
      expect(api.calls.some((call) => call.path === "/api/v1/cashflow/calendar" && call.search.includes("90"))).toBe(true);
    });
    await user.click(screen.getByRole("tab", { name: "Recurring items" }));
    await screen.findByRole("table", { name: "Recurring items" });
    expect(screen.queryByRole("combobox", { name: "Look ahead" })).not.toBeInTheDocument();
  });

  it("the figures for the period are a description list", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers(), "GET /api/v1/cashflow/calendar": () => json(CALENDAR) });
    renderApp("/plan/upcoming");
    await screen.findByRole("table", { name: "Upcoming days" });
    expect(screen.getAllByRole("term").map((term) => term.textContent)).toEqual(["Cash now", "Income expected", "Bills expected", "Net"]);
    // They are the shared Stat figures, not a private list.
    for (const term of screen.getAllByRole("term")) {
      expect(term).toHaveClass("stat__label");
    }
  });

  it("the tab is in the address", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
    });
    const { router } = renderApp("/plan/upcoming");
    const user = userEvent.setup();
    expect(await screen.findByRole("tab", { name: "Cash-flow calendar", selected: true })).toBeInTheDocument();
    expect(currentHref(router)).toBe("/plan/upcoming");
    await user.click(screen.getByRole("tab", { name: "Recurring items" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/upcoming?tab=recurring"); });
    expect(await screen.findByRole("table", { name: "Recurring items" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Cash-flow calendar" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/upcoming"); });
  });

  it("keeps keyboard focus on the tab chosen with the arrow keys", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
    });
    renderApp("/plan/upcoming");
    const user = userEvent.setup();
    (await screen.findByRole("tab", { name: "Cash-flow calendar", selected: true })).focus();
    await user.keyboard("{ArrowRight}");
    await screen.findByRole("table", { name: "Recurring items" });
    expect(screen.getByRole("tab", { name: "Recurring items", selected: true })).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    await screen.findByRole("table", { name: "Upcoming days" });
    expect(screen.getByRole("tab", { name: "Cash-flow calendar", selected: true })).toHaveFocus();
  });

  it("opens on the recurring items when the address asks for them", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
    });
    renderApp("/plan/upcoming?tab=recurring");
    expect(await screen.findByRole("tab", { name: "Recurring items", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Recurring items" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers(), "GET /api/v1/cashflow/calendar": () => json(CALENDAR) });
    const { container } = renderApp("/plan/upcoming");
    await screen.findByRole("table", { name: "Upcoming days" });
    await expectNoAxeViolations(container);
  });

  it("has no axe violations on the recurring items", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE, schedule("s-6", "Garden Club", { kind: "bill", status: "confirmed", status_source: "person", origin: "manual" })] }),
    });
    const { container } = renderApp("/plan/upcoming?tab=recurring");
    await screen.findByRole("table", { name: "Recurring items" });
    await expectNoAxeViolations(container);
  });

  it("lists recurring items and pauses one", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => new Response(null, { status: 404 }),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
      "PATCH /api/v1/recurring/:id": () => json({ ...SCHEDULE, active: false, status: "paused", status_source: "person" }),
    });
    renderApp("/plan/upcoming");
    const user = userEvent.setup();
    expect(screen.queryByRole("tab", { name: "Recurring and subscriptions" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("tab", { name: "Recurring items" }));
    const row = within(await screen.findByRole("table", { name: "Recurring items" })).getByRole("row", { name: /Streaming/ });
    expect(within(row).getByText("Found for you")).toBeInTheDocument();
    expect(within(row).getByText("Every month")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Pause Streaming" }));
    await vi.waitFor(() => { expect(api.callsTo("PATCH", "/api/v1/recurring/:id")).toHaveLength(1); });
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.body).toEqual({ active: false });
    // Suggested series are rows of the table now; the old pattern list is not asked for.
    expect(api.callsTo("GET", "/api/v1/recurring/candidates")).toHaveLength(0);
    expect(screen.queryByRole("heading", { name: "Suggestions" })).not.toBeInTheDocument();
  });

  it("a viewer sees the recurring items but no write controls", async () => {
    mockApi({
      ...referenceHandlers(),
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE, schedule("s-6", "Garden Club", { kind: "bill", status: "confirmed", status_source: "person", origin: "manual" })] }),
    });
    renderApp("/plan/upcoming?tab=recurring");
    const table = await screen.findByRole("table", { name: "Recurring items" });
    expect(within(table).getByRole("row", { name: /Streaming/ })).toBeInTheDocument();
    expect(within(table).getByRole("row", { name: /Garden Club/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New recurring item" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Find recurring items" })).toBeNull();
    expect(within(table).queryByRole("button")).toBeNull();
    expect(screen.getByRole("link", { name: "See subscriptions" })).toBeInTheDocument();
  });

  it("a row on an account the person may only view has no write controls", async () => {
    const handlers = referenceHandlers();
    const accounts = handlers["GET /api/v1/accounts"];
    mockApi({
      ...ownerSession(),
      ...handlers,
      // The same account, shared with the person to view only.
      "GET /api/v1/accounts": async (call) => {
        const response = accounts === undefined ? json({ items: [], next_cursor: null }) : await accounts(call);
        const body = (await response.json()) as { items: Record<string, unknown>[]; next_cursor: null };
        return json({ ...body, items: body.items.map((item) => ({ ...item, can_edit: false })) });
      },
      "GET /api/v1/cashflow/calendar": () => json(CALENDAR),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
    });
    renderApp("/plan/upcoming?tab=recurring");
    const table = await screen.findByRole("table", { name: "Recurring items" });
    await within(table).findByText("Checking");
    expect(within(table).queryByRole("button")).toBeNull();
  });

  it("the recurring tab shows kind and status and dismisses a detected item", async () => {
    const rows = [
      SCHEDULE,
      schedule("s-2", "Harbor Water", { kind: "bill", status: "confirmed", status_source: "person" }),
      schedule("s-3", "Payday", { kind: "income", status: "paused", status_source: "person", active: false, amount_min: money("2100.00"), amount_max: money("2100.00") }),
      schedule("s-4", "Puzzle Post", { kind: "other", status: "suggested", active: false }),
      schedule("s-5", "Old Gym", { kind: "subscription", status: "dismissed", status_source: "person", active: false }),
      schedule("s-6", "Garden Club", { kind: "bill", status: "confirmed", status_source: "person", origin: "manual" }),
    ];
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => new Response(null, { status: 404 }),
      "GET /api/v1/recurring": () => json({ items: rows }),
      "PATCH /api/v1/recurring/:id": () => json(SCHEDULE),
      "DELETE /api/v1/recurring/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/plan/upcoming");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Recurring items" }));
    const table = await screen.findByRole("table", { name: "Recurring items" });
    const headers = within(table).getAllByRole("columnheader").map((cell) => cell.textContent);
    const cell = (name: RegExp, column: string): HTMLElement => {
      const cells = within(within(table).getByRole("row", { name })).getAllByRole("cell");
      // The name is the row header, so the cells start one column later.
      const found = cells.at(headers.indexOf(column) - 1);
      if (found === undefined) {
        throw new Error(`no ${column} cell`);
      }
      return found;
    };
    expect(cell(/Streaming/, "Kind")).toHaveTextContent("Subscription");
    expect(cell(/Streaming/, "State")).toHaveTextContent("Found for you");
    expect(cell(/Harbor Water/, "Kind")).toHaveTextContent("Bill");
    expect(cell(/Harbor Water/, "State")).toHaveTextContent("Confirmed");
    expect(cell(/Payday/, "Kind")).toHaveTextContent("Income");
    expect(cell(/Payday/, "State")).toHaveTextContent("Paused");
    expect(cell(/Puzzle Post/, "Kind")).toHaveTextContent("Other");
    expect(cell(/Puzzle Post/, "State")).toHaveTextContent("Might be recurring");
    expect(cell(/Old Gym/, "State")).toHaveTextContent("Not a subscription");

    // The view of subscriptions and bills is one step away.
    expect(screen.getByRole("link", { name: "See subscriptions" })).toHaveAttribute("href", "/plan/subscriptions");

    // A found item is not deleted: the person says it is not recurring, and that sticks.
    const streaming = within(table).getByRole("row", { name: /Streaming/ });
    expect(within(streaming).queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
    // One the person added is still deleted; one already dismissed has nothing left to dismiss.
    expect(within(within(table).getByRole("row", { name: /Garden Club/ })).getByRole("button", { name: "Delete Garden Club" })).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /Old Gym/ })).queryByRole("button", { name: /Not recurring|Delete/ })).not.toBeInTheDocument();
    // A suggestion is confirmed, a paused item resumed, a dismissed one brought back: each is `active: true` by a person.
    expect(within(within(table).getByRole("row", { name: /Puzzle Post/ })).getByRole("button", { name: "Confirm Puzzle Post" })).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /Payday/ })).getByRole("button", { name: "Resume Payday" })).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /Old Gym/ })).getByRole("button", { name: "Reopen Old Gym" })).toBeInTheDocument();

    await user.click(within(streaming).getByRole("button", { name: "Not recurring: Streaming" }));
    await vi.waitFor(() => { expect(api.callsTo("DELETE", "/api/v1/recurring/:id")).toHaveLength(1); });
    expect(api.callsTo("DELETE", "/api/v1/recurring/:id")[0]?.path).toBe("/api/v1/recurring/s-1");

    await user.click(within(within(table).getByRole("row", { name: /Puzzle Post/ })).getByRole("button", { name: "Confirm Puzzle Post" }));
    await vi.waitFor(() => { expect(api.callsTo("PATCH", "/api/v1/recurring/:id")).toHaveLength(1); });
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.path).toBe("/api/v1/recurring/s-4");
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.body).toEqual({ active: true });

    // One the person added is deleted only once they say so.
    await user.click(within(within(table).getByRole("row", { name: /Garden Club/ })).getByRole("button", { name: "Delete Garden Club" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete Garden Club?" });
    expect(api.callsTo("DELETE", "/api/v1/recurring/:id")).toHaveLength(1);
    await user.click(within(confirm).getByRole("button", { name: "Delete item" }));
    await vi.waitFor(() => { expect(api.callsTo("DELETE", "/api/v1/recurring/:id")).toHaveLength(2); });
    expect(api.callsTo("DELETE", "/api/v1/recurring/:id")[1]?.path).toBe("/api/v1/recurring/s-6");
  });
});
