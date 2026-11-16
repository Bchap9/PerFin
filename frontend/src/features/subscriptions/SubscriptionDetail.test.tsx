// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import { ACCOUNT_CHECKING, money, referenceHandlers } from "../planning/testing";

type Row = Record<string, unknown>;

const STREAMLY: Row = {
  id: "s-1",
  account_id: ACCOUNT_CHECKING,
  name: "Streamly",
  merchant: "Streamly Media",
  kind: "subscription",
  kind_source: "detector",
  status: "confirmed",
  status_source: "person",
  cadence: "monthly",
  interval_count: 1,
  amount: money("-17.99"),
  amount_min: money("-17.99"),
  amount_max: money("-17.99"),
  monthly_cost: money("17.99"),
  yearly_cost: money("215.88"),
  next_due: "2026-12-10",
  last_seen_on: "2026-11-10",
  first_seen_on: "2026-08-10",
  occurrences: 4,
  open_events: 1,
  overdue: false,
  origin: "detected",
  payee_id: null,
  category_id: null,
  confidence: "0.90",
  price_change: null,
};

function event(id: string, kind: string, occurredOn: string, extra: Row = {}): Row {
  return { id, schedule_id: "s-1", name: "Streamly", kind, state: "seen", occurred_on: occurredOn, amount_before: null, amount_after: null, transaction_id: null, ...extra };
}

const DETAIL: Row = {
  subscription: STREAMLY,
  // Deliberately not in date order: the view sorts, newest first.
  charges: [
    { transaction_id: "t-2", posted_date: "2026-09-10", amount: money("-15.99") },
    { transaction_id: "t-4", posted_date: "2026-11-10", amount: money("-17.99") },
    { transaction_id: "t-1", posted_date: "2026-08-10", amount: money("-15.99") },
    { transaction_id: "t-3", posted_date: "2026-10-10", amount: money("-17.99") },
  ],
  events: [
    event("e-2", "price_changed", "2026-10-10", { state: "open", amount_before: money("-15.99"), amount_after: money("-17.99") }),
    event("e-1", "new", "2026-08-10"),
  ],
};

const TOTALS = { count: 1, currency: "USD", monthly: money("17.99"), yearly: money("215.88"), unconverted: [] };

function setup(options: { path?: string; session?: SessionOptions; detail?: Row; handlers?: Record<string, Handler> } = {}) {
  const api = mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner", ...options.session }),
    ...referenceHandlers(),
    "GET /api/v1/subscriptions": () =>
      json({ as_of: "2026-11-14", items: [STREAMLY], needs_look: 1, subscriptions: TOTALS, bills: { ...TOTALS, count: 0, monthly: money("0.00"), yearly: money("0.00") } }),
    "GET /api/v1/subscriptions/events": () => json({ items: [], next_cursor: null, open: 0 }),
    "GET /api/v1/subscriptions/:id": () => json(options.detail ?? DETAIL),
    ...options.handlers,
  });
  const app = renderApp(options.path ?? "/plan/subscriptions/s-1");
  // By exact path: the ":id" pattern would also count the calls for the events.
  const detailReads = (): number => api.calls.filter((call) => call.method === "GET" && call.path === "/api/v1/subscriptions/s-1").length;
  return { api, detailReads, user: userEvent.setup(), ...app };
}

/** A viewport below the 720px line: the series is a page of its own. Only the width query matches. */
function narrowScreen(): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({ matches: query === "(max-width: 719px)", media: query, addEventListener: () => undefined, removeEventListener: () => undefined })),
  );
}

describe("one subscription up close", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-16T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("the detail lists charges, price history and the next charge", async () => {
    const { detailReads } = setup();
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    expect(detailReads()).toBe(1);

    const facts = within(detail).getByRole("group", { name: "About Streamly" });
    expect(facts).toHaveTextContent("MerchantStreamly Media");
    expect(facts).toHaveTextContent("AccountChecking");
    expect(facts).toHaveTextContent("StatusConfirmed, set by you");
    expect(facts).toHaveTextContent("Per month$17.99");
    expect(facts).toHaveTextContent("Per year$215.88");
    expect(facts).toHaveTextContent("Next expected chargeDec 10, 2026");

    const charges = within(detail).getByRole("table", { name: "Charges" });
    const rows = within(charges).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual(["Nov 10, 2026$17.99", "Oct 10, 2026$17.99", "Sep 10, 2026$15.99", "Aug 10, 2026$15.99"]);

    const prices = within(detail).getByRole("region", { name: "Price history" });
    expect(within(prices).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Oct 10, 2026: $15.99 to $17.99"]);

    const changes = within(detail).getByRole("region", { name: "Changes" });
    expect(within(changes).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Aug 10, 2026: New"]);

    // The list stays behind the dialog.
    expect(screen.getByRole("heading", { level: 2, name: "Subscriptions" })).toBeInTheDocument();
  });

  it("says who set the status and when nothing is expected or has changed", async () => {
    setup({
      detail: { subscription: { ...STREAMLY, status: "active", status_source: "detector", next_due: null, merchant: null }, charges: [], events: [] },
    });
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    const facts = within(detail).getByRole("group", { name: "About Streamly" });
    expect(facts).toHaveTextContent("StatusFound for you, set by PerFin");
    expect(facts).toHaveTextContent("Next expected chargeNot known");
    expect(facts).not.toHaveTextContent("Merchant");
    expect(within(detail).getByText("No charges are linked to this yet.")).toBeInTheDocument();
    expect(within(detail).getByText("The price has not changed.")).toBeInTheDocument();
    expect(within(detail).getByText("Nothing else has changed.")).toBeInTheDocument();
  });

  it("links to its transactions", async () => {
    setup();
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    expect(within(detail).getByRole("link", { name: "See in transactions" })).toHaveAttribute("href", "/transactions?subscription=s-1");
  });

  it("opens from the list and closes back to it", async () => {
    const { user, router } = setup({ path: "/plan/subscriptions" });
    const active = await screen.findByRole("table", { name: "Active" });
    const link = within(active).getByRole("link", { name: "Streamly" });
    expect(link).toHaveAttribute("href", "/plan/subscriptions/s-1");
    await user.click(link);
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    expect(currentHref(router)).toBe("/plan/subscriptions/s-1");
    await user.click(within(detail).getByRole("button", { name: "Close" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/subscriptions"); });
    expect(screen.queryByRole("dialog", { name: "Streamly" })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Active" })).toBeInTheDocument();
  });

  it("is a page of its own on a narrow screen, with a way back", async () => {
    narrowScreen();
    setup();
    expect(await screen.findByRole("heading", { level: 2, name: "Streamly" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Active" })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Charges" })).toBeInTheDocument();
    // The way back is the breadcrumbs: Plan / Subscriptions / Streamly.
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumbs" });
    expect(within(crumbs).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Plan", "Subscriptions", "Streamly"]);
    expect(within(crumbs).getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/plan");
    expect(within(crumbs).getByRole("link", { name: "Subscriptions" })).toHaveAttribute("href", "/plan/subscriptions");
    expect(within(crumbs).getByText("Streamly")).toHaveAttribute("aria-current", "page");
  });

  it("has no axe violations as a page on a narrow screen", async () => {
    narrowScreen();
    const { container } = setup();
    await screen.findByRole("heading", { level: 2, name: "Streamly" });
    await screen.findByRole("table", { name: "Charges" });
    await expectNoAxeViolations(container);
  });

  it("a bill's breadcrumbs lead back to the Bills tab on a narrow screen", async () => {
    narrowScreen();
    const water = { ...STREAMLY, id: "b-1", name: "Harbor Water", merchant: "Harbor Water", kind: "bill" };
    setup({ path: "/plan/subscriptions/b-1?kind=bills", detail: { subscription: water, charges: [], events: [] } });
    const crumbs = await screen.findByRole("navigation", { name: "Breadcrumbs" });
    expect(within(crumbs).getByRole("link", { name: "Subscriptions" })).toHaveAttribute("href", "/plan/subscriptions?kind=bills");
  });

  it("a bill opened from the Bills tab closes back to the Bills tab", async () => {
    const water = { ...STREAMLY, id: "b-1", name: "Harbor Water", merchant: "Harbor Water", kind: "bill" };
    const { user, router } = setup({
      path: "/plan/subscriptions?kind=bills",
      detail: { subscription: water, charges: [], events: [] },
      handlers: {
        "GET /api/v1/subscriptions": () =>
          json({ as_of: "2026-11-14", items: [STREAMLY, water], needs_look: 0, subscriptions: TOTALS, bills: TOTALS }),
      },
    });
    const active = await screen.findByRole("table", { name: "Active" });
    const link = within(active).getByRole("link", { name: "Harbor Water" });
    expect(link).toHaveAttribute("href", "/plan/subscriptions/b-1?kind=bills");
    await user.click(link);
    const detail = await screen.findByRole("dialog", { name: "Harbor Water" });
    await user.click(within(detail).getByRole("button", { name: "Close" }));
    await vi.waitFor(() => { expect(currentHref(router)).toBe("/plan/subscriptions?kind=bills"); });
    expect(screen.getByRole("tab", { name: "Bills", selected: true })).toBeInTheDocument();
  });

  it("edits the name, the amount range and the cadence through the schedule form", async () => {
    const { api, detailReads, user } = setup({
      handlers: { "PATCH /api/v1/recurring/:id": () => json({}) },
    });
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    await user.click(within(detail).getByRole("button", { name: "Edit" }));
    const form = await screen.findByRole("dialog", { name: "Edit Streamly" });
    expect(within(form).getByLabelText(/Name/)).toHaveValue("Streamly");
    expect(within(form).getByLabelText(/Lowest amount/)).toHaveValue("17.99");
    expect(within(form).getByLabelText(/Repeats every/)).toHaveValue("1");
    // The account, the kind and the dates are not what this form changes.
    expect(within(form).queryByLabelText(/Account/)).not.toBeInTheDocument();
    expect(within(form).queryByLabelText(/Next due/)).not.toBeInTheDocument();

    await user.clear(within(form).getByLabelText(/Name/));
    await user.type(within(form).getByLabelText(/Name/), "Streamly Plus");
    await user.type(within(form).getByLabelText(/Highest amount/), "19.99");
    await user.selectOptions(within(form).getByLabelText(/Period/), "quarterly");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await vi.waitFor(() => { expect(api.callsTo("PATCH", "/api/v1/recurring/:id")).toHaveLength(1); });
    const call = api.callsTo("PATCH", "/api/v1/recurring/:id")[0];
    expect(call?.path).toBe("/api/v1/recurring/s-1");
    // A charge is money out: the range is stored negative, lowest number first.
    expect(call?.body).toEqual({
      name: "Streamly Plus",
      cadence: "quarterly",
      interval_count: 1,
      amount_min: money("-19.99"),
      amount_max: money("-17.99"),
    });
    await vi.waitFor(() => { expect(screen.queryByRole("dialog", { name: "Edit Streamly" })).not.toBeInTheDocument(); });
    // The series is read again after the change.
    await vi.waitFor(() => { expect(detailReads()).toBe(2); });
  });

  it("a viewer reads the series and cannot edit it", async () => {
    setup({ session: { role: "viewer" } });
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    expect(within(detail).getByRole("table", { name: "Charges" })).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("a member cannot edit a series on an account they may only read", async () => {
    setup({
      session: { role: "member" },
      handlers: {
        "GET /api/v1/accounts": () =>
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
          }),
      },
    });
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    // Once the accounts are in: the name of the account is on screen.
    expect(await within(detail).findByText("Checking")).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("a price change seen on one charge only is marked in the price history", async () => {
    const change = { previous: money("-15.99"), current: money("-17.99"), on: "2026-11-10", pending: true };
    setup({
      detail: {
        subscription: { ...STREAMLY, price_change: change },
        charges: [],
        events: [
          event("e-3", "price_changed", "2026-11-10", { state: "open", amount_before: money("-15.99"), amount_after: money("-17.99") }),
          event("e-2", "price_changed", "2026-06-10", { amount_before: money("-13.99"), amount_after: money("-15.99") }),
        ],
      },
    });
    const detail = await screen.findByRole("dialog", { name: "Streamly" });
    const prices = within(detail).getByRole("region", { name: "Price history" });
    expect(within(prices).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Nov 10, 2026: $15.99 to $17.99, not confirmed yet",
      "Jun 10, 2026: $13.99 to $15.99",
    ]);
  });

  it("says so when the series cannot be read", async () => {
    setup({ handlers: { "GET /api/v1/subscriptions/:id": () => json({ type: "urn:perfin:problem:not-found", title: "Not found", status: 404, detail: "No such series." }, 404) } });
    const detail = await screen.findByRole("dialog", { name: "Subscription" });
    expect(await within(detail).findByRole("alert")).toHaveTextContent("Could not load this series.");
  });
});
