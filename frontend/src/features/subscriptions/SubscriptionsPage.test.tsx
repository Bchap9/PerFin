// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import { ACCOUNT_CHECKING, money, referenceHandlers } from "../planning/testing";

type Row = Record<string, unknown>;

function series(id: string, name: string, extra: Row = {}): Row {
  return {
    id,
    account_id: ACCOUNT_CHECKING,
    name,
    merchant: name,
    kind: "subscription",
    kind_source: "detector",
    status: "active",
    status_source: "detector",
    cadence: "monthly",
    interval_count: 1,
    amount: money("-15.99"),
    amount_min: money("-15.99"),
    amount_max: money("-15.99"),
    monthly_cost: money("15.99"),
    yearly_cost: money("191.88"),
    next_due: "2026-12-30",
    last_seen_on: "2026-10-30",
    first_seen_on: "2026-01-30",
    occurrences: 10,
    open_events: 0,
    overdue: false,
    origin: "detected",
    payee_id: null,
    category_id: null,
    confidence: "0.90",
    price_change: null,
    ...extra,
  };
}

function totals(count: number, monthly: string, yearly: string, unconverted: string[] = []): Row {
  return { count, currency: "USD", monthly: money(monthly), yearly: money(yearly), unconverted };
}

const STREAMLY = series("s-1", "Streamly", { next_due: "2026-11-20" });
const CLOUD = series("s-2", "Cloud Locker", {
  amount: money("-2.99"),
  monthly_cost: money("2.99"),
  yearly_cost: money("35.88"),
});
const GYM = series("s-3", "Gym Corner", {
  amount: money("-480.00"),
  cadence: "yearly",
  monthly_cost: money("40.00"),
  yearly_cost: money("480.00"),
  status: "confirmed",
  status_source: "person",
  next_due: "2027-03-01",
});
const WATER = series("b-1", "Harbor Water", {
  kind: "bill",
  amount: money("-61.20"),
  monthly_cost: money("61.20"),
  yearly_cost: money("734.40"),
  next_due: "2026-11-28",
});
const MAYBE = series("s-4", "Puzzle Post", { status: "suggested", open_events: 0, next_due: "2026-11-25" });

/** The account of every series here, as a member who may only read it sees it. */
const READ_ONLY_ACCOUNTS: Handler = () =>
  json({
    items: [
      {
        id: ACCOUNT_CHECKING,
        name: "Checking",
        nickname: null,
        type: "checking",
        currency: "USD",
        balance: money("1200.50"),
        balance_date: "2026-11-03",
        is_asset: true,
        is_closed: false,
        include_in_net_worth: true,
        manual_balance: false,
        can_edit: false,
        asset_kind: null,
        subtype: null,
        subtype_label: null,
        tax_treatment: null,
        closed_on: null,
        institution_id: null,
        number_last4: null,
        opened_on: null,
        created_at: "2026-11-01T10:00:00Z",
        updated_at: "2026-11-01T10:00:00Z",
      },
    ],
    next_cursor: null,
  });

const MISSED_EVENT: Row = { id: "e-2", schedule_id: "s-1", name: "Streamly", kind: "missed", state: "open", occurred_on: "2026-11-10", amount_before: null, amount_after: null, transaction_id: null };

function listBody(items: Row[], extra: Row = {}): Row {
  return {
    as_of: "2026-11-14",
    items,
    needs_look: 0,
    subscriptions: totals(3, "58.98", "707.76"),
    bills: totals(1, "61.20", "734.40"),
    ...extra,
  };
}

function setup(items: Row[], options: { path?: string; session?: SessionOptions; events?: Row[]; list?: Row; handlers?: Record<string, Handler> } = {}) {
  const state = { items, events: options.events ?? [] };
  const api = mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner", ...options.session }),
    ...referenceHandlers(),
    "GET /api/v1/subscriptions": () => json(listBody(state.items, options.list)),
    "GET /api/v1/subscriptions/events": () => json({ items: state.events, next_cursor: null, open: state.events.length }),
    ...options.handlers,
  });
  // Through the router: a series' name is a link to it up close.
  const app = renderApp(options.path ?? "/plan/subscriptions");
  return { api, state, user: userEvent.setup(), ...app };
}

describe("subscriptions view", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-16T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the monthly and yearly cost of the tab", async () => {
    setup([STREAMLY, CLOUD, GYM, WATER]);
    const cost = await screen.findByRole("group", { name: "Cost of subscriptions" });
    expect(within(cost).getByText("Per month")).toBeInTheDocument();
    expect(within(cost).getByText("$58.98")).toBeInTheDocument();
    expect(within(cost).getByText("Per year")).toBeInTheDocument();
    expect(within(cost).getByText("$707.76")).toBeInTheDocument();
    expect(within(cost).getByText("3 subscriptions")).toBeInTheDocument();
    const active = screen.getByRole("table", { name: "Active" });
    expect(within(active).getByRole("row", { name: /Streamly/ })).toHaveTextContent("Checking");
    expect(within(active).getByRole("row", { name: /Streamly/ })).toHaveTextContent("Every month");
    expect(within(active).queryByRole("row", { name: /Harbor Water/ })).not.toBeInTheDocument();
    const soon = screen.getByRole("region", { name: "Next 30 days" });
    expect(within(soon).getAllByRole("listitem")).toHaveLength(1);
    expect(within(soon).getByRole("listitem")).toHaveTextContent("Nov 20, 2026");
    expect(within(soon).getByRole("listitem")).toHaveTextContent("Streamly");
    expect(within(soon).getByRole("listitem")).toHaveTextContent("$15.99");
  });

  it("bills have their own figures", async () => {
    const { user } = setup([STREAMLY, CLOUD, GYM, WATER]);
    await user.click(await screen.findByRole("tab", { name: "Bills" }));
    const cost = await screen.findByRole("group", { name: "Cost of bills" });
    expect(within(cost).getByText("$61.20")).toBeInTheDocument();
    expect(within(cost).getByText("$734.40")).toBeInTheDocument();
    expect(within(cost).getByText("1 bill")).toBeInTheDocument();
    const active = screen.getByRole("table", { name: "Active" });
    expect(within(active).getByRole("row", { name: /Harbor Water/ })).toBeInTheDocument();
    expect(within(active).queryByRole("row", { name: /Streamly/ })).not.toBeInTheDocument();
  });

  it("a suggested series can be confirmed or dismissed", async () => {
    const { api, state, user } = setup([STREAMLY, MAYBE], {
      handlers: {
        "POST /api/v1/subscriptions/:id/decision": (call) => {
          const body = call.body as { decision: string };
          const status = body.decision === "confirm" ? "confirmed" : "dismissed";
          const changed = { ...MAYBE, status, status_source: "person" };
          state.items = [STREAMLY, changed];
          return json(changed);
        },
      },
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    const item = within(look).getByRole("listitem");
    expect(item).toHaveTextContent("Puzzle Post");
    expect(item).toHaveTextContent("Might be a subscription");
    expect(within(screen.getByRole("table", { name: "Active" })).queryByRole("row", { name: /Puzzle Post/ })).not.toBeInTheDocument();

    await user.click(within(item).getByRole("button", { name: "Confirm Puzzle Post" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")[0]?.path).toBe("/api/v1/subscriptions/s-4/decision");
    expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")[0]?.body).toEqual({ decision: "confirm" });
    expect(await within(screen.getByRole("table", { name: "Active" })).findByRole("row", { name: /Puzzle Post/ })).toHaveTextContent("Confirmed");
    expect(screen.queryByRole("region", { name: "Needs a look" })).not.toBeInTheDocument();
  });

  it("a dismissed series moves to the folded section", async () => {
    const { api, state, user } = setup([STREAMLY, MAYBE], {
      handlers: {
        "POST /api/v1/subscriptions/:id/decision": () => {
          const changed = { ...MAYBE, status: "dismissed", status_source: "person" };
          state.items = [STREAMLY, changed];
          return json(changed);
        },
      },
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    await user.click(within(look).getByRole("button", { name: "Not a subscription: Puzzle Post" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")[0]?.body).toEqual({ decision: "dismiss" });
    const folded = await screen.findByRole("group", { name: "Ended and dismissed" });
    expect(folded).not.toHaveAttribute("open");
    expect(folded).toHaveTextContent("Puzzle Post");
    expect(folded).toHaveTextContent("Not a subscription");
    expect(screen.queryByRole("region", { name: "Needs a look" })).not.toBeInTheDocument();
  });

  it("a price change is shown in words and acknowledged", async () => {
    const changed = { ...STREAMLY, open_events: 1, amount: money("-17.99") };
    const { api, state, user } = setup([changed, CLOUD], {
      events: [
        {
          id: "e-1",
          schedule_id: "s-1",
          name: "Streamly",
          kind: "price_changed",
          state: "open",
          occurred_on: "2026-11-10",
          amount_before: money("-15.99"),
          amount_after: money("-17.99"),
          transaction_id: null,
        },
      ],
      handlers: {
        "POST /api/v1/subscriptions/events/seen": () => {
          state.items = [{ ...changed, open_events: 0 }, CLOUD];
          state.events = [];
          return json({ seen: 1 });
        },
      },
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    const item = within(look).getByRole("listitem");
    expect(await within(item).findByText("Price went from $15.99 to $17.99")).toBeInTheDocument();
    expect(within(item).queryByRole("button", { name: /Confirm/ })).not.toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/subscriptions/events")[0]?.search).toContain("state=open");
    await user.click(within(item).getByRole("button", { name: "Got it: Streamly" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/subscriptions/events/seen")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/subscriptions/events/seen")[0]?.body).toEqual({ ids: ["e-1"] });
    await vi.waitFor(() => { expect(screen.queryByRole("region", { name: "Needs a look" })).not.toBeInTheDocument(); });
    expect(within(screen.getByRole("table", { name: "Active" })).getByRole("row", { name: /Streamly/ })).toBeInTheDocument();
  });

  it("sorts by yearly cost", async () => {
    const { user } = setup([CLOUD, GYM, STREAMLY]);
    const active = await screen.findByRole("table", { name: "Active" });
    const names = (): (string | null)[] =>
      within(active)
        .getAllByRole("rowheader")
        .map((cell) => cell.querySelector(".subs-name")?.textContent ?? null);
    expect(names()).toEqual(["Gym Corner", "Streamly", "Cloud Locker"]);
    expect(within(active).getByRole("columnheader", { name: /Per year/ })).toHaveAttribute("aria-sort", "descending");
    await user.click(within(active).getByRole("button", { name: /Per year/ }));
    expect(names()).toEqual(["Cloud Locker", "Streamly", "Gym Corner"]);
    expect(within(active).getByRole("columnheader", { name: /Per year/ })).toHaveAttribute("aria-sort", "ascending");
  });

  it("the row menu pauses, ends and says which of the two it is", async () => {
    const { api, user } = setup([STREAMLY], {
      handlers: {
        "POST /api/v1/subscriptions/:id/decision": () => json(STREAMLY),
        "PATCH /api/v1/recurring/:id": () => json({}),
      },
    });
    const active = await screen.findByRole("table", { name: "Active" });
    const open = async (): Promise<HTMLElement> => {
      await user.click(within(active).getByRole("button", { name: "More for Streamly" }));
      return within(active).getByRole("group", { name: "Actions for Streamly" });
    };
    const bodies = (): unknown[] => api.callsTo("POST", "/api/v1/subscriptions/:id/decision").map((call) => call.body);
    await user.click(within(await open()).getByRole("button", { name: "Pause" }));
    await vi.waitFor(() => { expect(bodies()).toHaveLength(1); });
    await user.click(within(await open()).getByRole("button", { name: "Ended" }));
    await vi.waitFor(() => { expect(bodies()).toHaveLength(2); });
    const menu = await open();
    expect(within(menu).queryByRole("button", { name: "Reopen" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    const listReads = api.callsTo("GET", "/api/v1/subscriptions").length;
    await user.click(within(menu).getByRole("button", { name: "This is a bill" }));
    // Which of the two it is says nothing about the status: the kind alone is set, not a decision.
    await vi.waitFor(() => { expect(api.callsTo("PATCH", "/api/v1/recurring/:id")).toHaveLength(1); });
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.path).toBe("/api/v1/recurring/s-1");
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.body).toEqual({ kind: "bill" });
    expect(bodies()).toEqual([{ decision: "pause" }, { decision: "end" }]);
    await vi.waitFor(() => { expect(api.callsTo("GET", "/api/v1/subscriptions").length).toBeGreaterThan(listReads); });
  });

  it("a paused series resumes and an ended one reopens", async () => {
    const paused = { ...STREAMLY, status: "paused", status_source: "person" };
    const ended = { ...CLOUD, status: "ended", status_source: "person" };
    const { api, user } = setup([paused, ended], {
      handlers: { "POST /api/v1/subscriptions/:id/decision": () => json(STREAMLY) },
    });
    const active = await screen.findByRole("table", { name: "Active" });
    expect(within(active).getByRole("row", { name: /Streamly/ })).toHaveTextContent("Paused");
    await user.click(within(active).getByRole("button", { name: "More for Streamly" }));
    await user.click(within(active).getByRole("button", { name: "Resume" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")).toHaveLength(1); });
    const folded = screen.getByRole("group", { name: "Ended and dismissed" });
    await user.click(within(folded).getByRole("button", { name: "Reopen Cloud Locker" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision")).toHaveLength(2); });
    expect(api.callsTo("POST", "/api/v1/subscriptions/:id/decision").map((call) => [call.path, call.body])).toEqual([
      ["/api/v1/subscriptions/s-1/decision", { decision: "resume" }],
      ["/api/v1/subscriptions/s-2/decision", { decision: "reopen" }],
    ]);
  });

  it("a viewer sees no buttons", async () => {
    const changed = { ...STREAMLY, open_events: 1 };
    setup([changed, MAYBE, { ...CLOUD, status: "ended" }], {
      session: { role: "viewer" },
      events: [MISSED_EVENT],
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    expect(await within(look).findByText("Expected on Nov 10, 2026, not seen")).toBeInTheDocument();
    expect(within(look).getAllByRole("listitem")).toHaveLength(2);
    expect(within(look).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Confirm/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Not a subscription/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Got it/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^More for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reopen/ })).not.toBeInTheDocument();
  });

  it("series that could not be converted are named", async () => {
    // The API names them by id; the page says their names.
    const lingua = series("s-8", "Lingua Club");
    const brise = series("s-9", "Brise Radio");
    setup([STREAMLY, lingua, brise], { list: { subscriptions: totals(1, "15.99", "191.88", ["s-8", "s-9", "s-gone"]) } });
    const cost = await screen.findByRole("group", { name: "Cost of subscriptions" });
    expect(within(cost).getByText("Not included: Lingua Club")).toBeInTheDocument();
    expect(within(cost).getByText("Not included: Brise Radio")).toBeInTheDocument();
    expect(within(cost).getByText("Not included: one that is not listed here")).toBeInTheDocument();
    expect(cost).not.toHaveTextContent("s-8");
    expect(cost).not.toHaveTextContent("s-gone");
  });

  it("asks for every status, the dismissed ones too", async () => {
    const { api } = setup([STREAMLY]);
    await screen.findByRole("table", { name: "Active" });
    const search = new URLSearchParams(api.callsTo("GET", "/api/v1/subscriptions")[0]?.search ?? "");
    expect(search.getAll("status")).toEqual(["suggested", "active", "confirmed", "paused", "ended", "dismissed"]);
  });

  it("a member sees no buttons for a series on an account they cannot edit", async () => {
    setup([{ ...STREAMLY, open_events: 1 }, MAYBE, GYM, { ...CLOUD, status: "ended" }], {
      session: { role: "member" },
      events: [MISSED_EVENT],
      handlers: { "GET /api/v1/accounts": READ_ONLY_ACCOUNTS },
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    expect(await within(look).findByText("Expected on Nov 10, 2026, not seen")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Active" })).getByRole("row", { name: /Gym Corner/ })).toHaveTextContent("Checking");
    expect(within(look).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Confirm/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Not a subscription/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Got it/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^More for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reopen/ })).not.toBeInTheDocument();
  });

  it("says so when the changes cannot be read", async () => {
    setup([{ ...STREAMLY, status: "confirmed", open_events: 1 }, CLOUD], {
      handlers: {
        "GET /api/v1/subscriptions/events": () => json({ type: "urn:perfin:problem:internal", title: "Server error", status: 500, detail: "Broken." }, 500),
      },
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    expect(await within(look).findByText("Something changed")).toBeInTheDocument();
    expect(await screen.findByText("Could not load what changed.")).toHaveAttribute("role", "alert");
    // What changed is not at hand, so there is nothing to mark as seen.
    expect(within(look).queryByRole("button", { name: /Got it/ })).not.toBeInTheDocument();
  });

  it("a price change seen on one charge only is not confirmed yet", async () => {
    const change = { previous: money("-10.99"), current: money("-12.99"), on: "2026-11-10", pending: true };
    setup([{ ...STREAMLY, open_events: 1, price_change: change }], {
      events: [
        { id: "e-3", schedule_id: "s-1", name: "Streamly", kind: "price_changed", state: "open", occurred_on: "2026-11-10", amount_before: money("-10.99"), amount_after: money("-12.99"), transaction_id: null },
      ],
    });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    expect(await within(look).findByText("Price went from $10.99 to $12.99, not confirmed yet")).toBeInTheDocument();
  });

  it("the empty state", async () => {
    setup([], { list: { subscriptions: totals(0, "0.00", "0.00"), bills: totals(0, "0.00", "0.00") } });
    expect(await screen.findByText("No subscriptions found yet. They appear after a few statements are imported.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Active" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs a look" })).not.toBeInTheDocument();
  });

  it("the per-month and per-year figures are a description list", async () => {
    setup([STREAMLY, CLOUD, GYM, WATER]);
    const cost = await screen.findByRole("group", { name: "Cost of subscriptions" });
    expect(within(cost).getAllByRole("term").map((term) => term.textContent)).toEqual(["Per month", "Per year"]);
    // They are the shared Stat figures, not a private list.
    for (const term of within(cost).getAllByRole("term")) {
      expect(term).toHaveClass("stat__label");
    }
    expect(within(cost).getAllByRole("definition").map((value) => value.textContent)).toEqual(["$58.98", "$707.76"]);
  });

  it("the tab between subscriptions and bills is in the address", async () => {
    const { router, user } = setup([STREAMLY, WATER]);
    expect(await screen.findByRole("tab", { name: "Subscriptions", selected: true })).toBeInTheDocument();
    expect(currentHref(router)).toBe("/plan/subscriptions");
    await user.click(screen.getByRole("tab", { name: "Bills" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/subscriptions?kind=bills"); });
    expect(await screen.findByRole("group", { name: "Cost of bills" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Subscriptions" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/subscriptions"); });
  });

  it("opens on the bills when the address asks for them", async () => {
    setup([STREAMLY, WATER], { path: "/plan/subscriptions?kind=bills" });
    expect(await screen.findByRole("tab", { name: "Bills", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Cost of bills" })).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Active" })).getByRole("row", { name: /Harbor Water/ })).toBeInTheDocument();
  });

  it("a series' name opens it under Plan", async () => {
    setup([STREAMLY]);
    const active = await screen.findByRole("table", { name: "Active" });
    expect(within(active).getByRole("link", { name: "Streamly" })).toHaveAttribute("href", "/plan/subscriptions/s-1");
    const soon = screen.getByRole("region", { name: "Next 30 days" });
    expect(within(soon).getByRole("link", { name: "Streamly" })).toHaveAttribute("href", "/plan/subscriptions/s-1");
  });

  it("has no axe violations", async () => {
    const { container } = setup([{ ...STREAMLY, open_events: 1 }, MAYBE, CLOUD, GYM, { ...series("s-7", "Old Radio"), status: "ended" }], { events: [MISSED_EVENT] });
    const look = await screen.findByRole("region", { name: "Needs a look" });
    await within(look).findByText("Expected on Nov 10, 2026, not seen");
    await expectNoAxeViolations(container);
  });
});
