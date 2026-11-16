// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";

import { expectNoAxeViolations } from "../../test/axe";
import { json, mockApi, renderApp, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, CAT_DINING, CAT_FOOD, CAT_RENT, money, ownerSession, referenceHandlers } from "../planning/testing";

const STAMP = "2026-11-01T10:00:00Z";

const SUMMARY = { cards: 4, documents: 3, loose: 0, subscriptions: 1, suggestions: 2, uncategorised: 7 };

function document(id: string, filename: string, created_at: string) {
  return {
    id,
    original_filename: filename,
    created_at,
    kind: "statement",
    mime_detected: "application/pdf",
    period_start: null,
    period_end: null,
    previewable: true,
    source: "upload",
    status: "needs_review",
  };
}

function card(id: string, filename: string, action: string, created_at: string) {
  return { action, action_item_id: `i-${id}`, document: document(id, filename, created_at), items: [], notices: 0, open: 1 };
}

const CARDS = [
  card("d-note", "Receipt scan.jpg", "acknowledge", "2026-11-01T09:00:00Z"),
  card("d-import", "Card export.csv", "map_columns", "2026-11-03T09:00:00Z"),
  card("d-money", "Everyday statement.pdf", "reconcile", "2026-11-05T09:00:00Z"),
  card("d-late", "Brokerage.pdf", "choose_account", "2026-11-09T09:00:00Z"),
];

function report(from: string, total: string, items: { key: string | null; name: string | null; amount: string }[]) {
  return {
    currency: "USD",
    date_from: from,
    date_to: from,
    depth: 1,
    flow: "spending",
    group_by: "category",
    include_pending: false,
    total: money(total),
    unconverted: [],
    items: items.map((item) => ({ ...item, amount: money(item.amount) })),
  };
}

function transaction(id: string, patch: Record<string, unknown>) {
  return {
    id,
    account_id: ACCOUNT_CHECKING,
    amount: money("-12.00"),
    posted_date: "2026-11-14",
    raw_payee: "CAFE UNO 0042",
    merchant: null,
    memo: null,
    notes: null,
    category_id: null,
    category_source: null,
    suggested_category_id: null,
    payee_id: null,
    review_state: "none",
    status: "cleared",
    tag_ids: [],
    splits: [],
    transfer: null,
    confidence: null,
    effective_date: null,
    source_document_id: null,
    created_at: STAMP,
    updated_at: STAMP,
    ...patch,
  };
}

const SUBSCRIPTION = {
  id: "s-stream",
  account_id: ACCOUNT_CHECKING,
  name: "Streamly Plus",
  merchant: "Streamly",
  kind: "subscription",
  kind_source: "detector",
  status: "confirmed",
  status_source: "person",
  cadence: "monthly",
  interval_count: 1,
  amount: money("-17.99"),
  amount_min: money("-15.99"),
  amount_max: money("-17.99"),
  monthly_cost: money("17.99"),
  yearly_cost: money("215.88"),
  category_id: null,
  confidence: null,
  first_seen_on: "2026-01-12",
  last_seen_on: "2026-10-12",
  next_due: "2026-11-20",
  occurrences: 10,
  open_events: 0,
  origin: "detected",
  overdue: false,
  payee_id: null,
  price_change: { previous: money("-15.99"), current: money("-17.99"), on: "2026-10-12", pending: false },
};

const SCHEDULE = {
  id: "r-power",
  account_id: ACCOUNT_CHECKING,
  name: "Riverside Power",
  merchant: null,
  kind: "bill",
  status: "active",
  status_source: "detector",
  active: true,
  cadence: "monthly",
  interval_count: 1,
  amount_min: money("-105.00"),
  amount_max: money("-125.00"),
  category_id: null,
  last_seen_on: "2026-10-07",
  next_due: "2026-12-07",
  origin: "detected",
  payee_id: null,
};

function handlers(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    ...ownerSession({ displayName: "Robin Vale" }),
    ...referenceHandlers(),
    "GET /api/v1/inbox/summary": () => json(SUMMARY),
    "GET /api/v1/review/cards": () => json({ cards: CARDS, loose: [], next_cursor: null }),
    "GET /api/v1/networth/latest": () =>
      json({
        as_of: "2026-11-16",
        currency: "USD",
        net: money("184620.00"),
        assets: money("200000.00"),
        liabilities: money("-15380.00"),
        accounts: [],
        by_class: {},
        by_type: {},
        last_snapshot_date: null,
        scope: "household",
        unconverted: [],
        unpriced_security_ids: [],
      }),
    "GET /api/v1/networth": () =>
      json({
        currency: "USD",
        change: money("20620.00"),
        date_from: "2025-11-16",
        date_to: "2026-11-16",
        granularity: "month",
        range: "1y",
        scope: "household",
        points: [
          { as_of: "2025-11-30", net: money("164000.00"), assets: money("0"), liabilities: money("0"), source: "snapshot" },
          { as_of: "2026-10-31", net: money("182480.00"), assets: money("0"), liabilities: money("0"), source: "snapshot" },
          { as_of: "2026-11-16", net: money("184620.00"), assets: money("0"), liabilities: money("0"), source: "computed" },
        ],
      }),
    "GET /api/v1/reports/spending": (call) =>
      call.search.includes("date_from=2026-11-01")
        ? json(
            report("2026-11-01", "920.00", [
              { key: CAT_RENT, name: "Rent", amount: "600.00" },
              { key: CAT_FOOD, name: "Food", amount: "250.00" },
              { key: null, name: null, amount: "70.00" },
            ]),
          )
        : json(report("2026-10-01", "1000.00", [])),
    "GET /api/v1/subscriptions": () =>
      json({
        as_of: "2026-11-16",
        items: [SUBSCRIPTION],
        needs_look: 0,
        bills: { count: 0, monthly: money("0"), yearly: money("0") },
        subscriptions: { count: 1, monthly: money("17.99"), yearly: money("215.88") },
      }),
    "GET /api/v1/recurring": () => json({ items: [SCHEDULE], next_cursor: null }),
    "GET /api/v1/transactions": () =>
      json({
        next_cursor: null,
        items: [
          transaction("t-cafe", { merchant: "Cafe Uno", category_id: CAT_DINING, category_source: "person" }),
          transaction("t-guess", { raw_payee: "GREENLEAF MKT", suggested_category_id: CAT_FOOD, category_source: null }),
        ],
      }),
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 10, 16, 9, 30));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Home", () => {
  it("greets the person and offers to add documents", async () => {
    mockApi(handlers());
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Good morning, Robin Vale" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText("Your money at a glance.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add documents" })).toHaveAttribute("href", "/inbox");
  });

  it("shows net worth with its change and a sparkline label", async () => {
    mockApi(handlers());
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Net worth" });
    expect(await within(panel).findByText("$184,620.00")).toBeInTheDocument();
    expect(within(panel).getByText("Up $2,140.00 this month")).toBeInTheDocument();
    expect(
      await within(panel).findByRole("img", { name: "Net worth over the last 12 months, from $164,000.00 to $184,620.00" }),
    ).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Accounts" })).toHaveAttribute("href", "/accounts");
  });

  it("needs you lists the first three cards in Inbox order and the uncategorised line", async () => {
    mockApi(handlers());
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Needs you" });
    const list = await within(panel).findByRole("list", { name: "What needs you" });
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/inbox?document=d-money",
      "/inbox?document=d-import",
      "/inbox?document=d-late",
      "/transactions?category=none",
    ]);
    expect(links[0]).toHaveTextContent("Everyday statement.pdf");
    expect(links[0]).toHaveTextContent("Check the balance against the statement");
    expect(links[3]).toHaveTextContent("7 transactions have no category");
    expect(links[3]).toHaveTextContent("2 have a suggestion");
    expect(within(panel).queryByText("Receipt scan.jpg")).not.toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Open Inbox" })).toHaveAttribute("href", "/inbox");
  });

  it("needs you says nothing is waiting", async () => {
    mockApi(
      handlers({
        "GET /api/v1/inbox/summary": () => json({ cards: 0, documents: 0, loose: 0, subscriptions: 0, suggestions: 0, uncategorised: 0 }),
        "GET /api/v1/review/cards": () => json({ cards: [], loose: [], next_cursor: null }),
      }),
    );
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Needs you" });
    expect(await within(panel).findByText("Nothing is waiting. Every document is imported.")).toBeInTheDocument();
    expect(within(panel).queryByRole("link", { name: /no category/ })).not.toBeInTheDocument();
  });

  it("spending compares with the same day last month", async () => {
    const api = mockApi(handlers());
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Spending in November" });
    expect(await within(panel).findByText("$920.00")).toBeInTheDocument();
    expect(within(panel).getByText("8% less than this point in October")).toBeInTheDocument();
    const legend = within(panel).getByRole("list", { name: "Spending by category this month" });
    expect(within(legend).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Rent$600.00",
      "Food$250.00",
      "Uncategorised$70.00",
    ]);
    expect(within(panel).getByRole("link", { name: "Reports" })).toHaveAttribute("href", "/reports");
    const searches = api.callsTo("GET", "/api/v1/reports/spending").map((call) => call.search);
    expect(searches.some((search) => search.includes("date_from=2026-11-01") && search.includes("date_to=2026-11-16"))).toBe(true);
    expect(searches.some((search) => search.includes("date_from=2026-10-01") && search.includes("date_to=2026-10-16"))).toBe(true);
  });

  it("coming up shows a price change note", async () => {
    mockApi(handlers());
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Coming up" });
    const items = await within(panel).findAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Streamly Plus");
    expect(items[0]).toHaveTextContent("went up $2.00 last month");
    expect(items[0]).toHaveTextContent("$17.99");
    expect(items[1]).toHaveTextContent("Riverside Power");
    expect(items[1]).toHaveTextContent("usually $105.00 to $125.00");
    expect(items[1]).toHaveTextContent("~$115");
    expect(within(panel).getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/plan/upcoming");
  });

  it("latest transactions open the drawer link", async () => {
    mockApi(handlers());
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Latest transactions" });
    const cafe = await within(panel).findByRole("link", { name: /Cafe Uno/ });
    expect(cafe).toHaveAttribute("href", "/transactions?tx=t-cafe");
    expect(cafe).toHaveTextContent("CAFE UNO 0042");
    expect(cafe).toHaveTextContent("Dining");
    expect(cafe).toHaveTextContent("You chose this");
    const guess = within(panel).getByRole("link", { name: /GREENLEAF MKT/ });
    expect(guess).toHaveTextContent("Food?");
    expect(guess).toHaveTextContent("Suggestion");
    expect(within(panel).getByRole("link", { name: "All transactions" })).toHaveAttribute("href", "/transactions");
  });

  it("a household with no accounts sees Start here", async () => {
    mockApi(handlers({ "GET /api/v1/accounts": () => json({ items: [], next_cursor: null }) }));
    renderApp("/");
    expect(await screen.findByRole("region", { name: "Start here" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Net worth" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Latest transactions" })).not.toBeInTheDocument();
  });

  it("one failing panel does not hide the others", async () => {
    const api = mockApi(
      handlers({
        "GET /api/v1/networth/latest": () => json({ type: "urn:perfin:problem:server-error", title: "Boom", status: 500 }, 500),
      }),
    );
    renderApp("/");
    const panel = await screen.findByRole("region", { name: "Net worth" });
    expect(await within(panel).findByText(/Could not load net worth/)).toBeInTheDocument();
    const retry = within(panel).getByRole("button", { name: "Retry" });
    expect(await screen.findByText("8% less than this point in October")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Cafe Uno/ })).toBeInTheDocument();
    const before = api.callsTo("GET", "/api/v1/networth/latest").length;
    retry.click();
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/networth/latest").length).toBeGreaterThan(before);
    });
  });

  it("has no axe violations", async () => {
    mockApi(handlers());
    const { container } = renderApp("/");
    await screen.findByText("Up $2,140.00 this month");
    await screen.findByText("8% less than this point in October");
    await screen.findByRole("link", { name: /Cafe Uno/ });
    await screen.findByText(/went up \$2\.00 last month/);
    await screen.findByText("Everyday statement.pdf");
    await expectNoAxeViolations(container);
  });
});
