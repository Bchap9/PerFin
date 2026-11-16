// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { formatDate } from "../../lib/dates";
import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import type { LearnedCategory } from "../categories/api";
import type { DocumentRow } from "../imports/api";
import type { ReviewCard, ReviewItem } from "../review/api";
import type { Subscription, SubscriptionEvent } from "../subscriptions/api";
import { account, lookupHandlers, NOW, summary, tx } from "../transactions/fixtures";
import type { Transaction } from "../transactions/api";
import { recentImports } from "./RecentImports";

// Invented fixtures: no real people, merchants or account numbers.

const money = (amount: string) => ({ amount, currency: "USD" });

function document(id: string, name: string, createdAt: string): ReviewCard["document"] {
  return {
    id,
    original_filename: name,
    kind: "statement",
    status: "needs_review",
    mime_detected: "text/csv",
    source: "upload",
    created_at: createdAt,
    period_start: null,
    period_end: null,
    previewable: false,
  };
}

function item(id: string, documentId: string | null, extra: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id,
    kind: "unparsed",
    state: "open",
    document_id: documentId,
    payload: { reason: "format_not_supported" },
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: null,
    can_resolve: true,
    ...extra,
  };
}

function card(doc: ReviewCard["document"], action: ReviewCard["action"]): ReviewCard {
  const one = item(`r-${doc.id}`, doc.id, { document: doc });
  return { document: doc, items: [one], action, action_item_id: one.id, open: 1, notices: 0 };
}

const NOTE_OLD = card(document("d-1", "harbor-notes.csv", "2026-10-01T09:00:00Z"), "acknowledge");
const MONEY_NEW = card(document("d-2", "maple-card-october.csv", "2026-11-02T09:00:00Z"), "reconcile");
const LOOSE = item("r-loose", null, { kind: "unknown_owner", payload: { reason: "unknown_sender" } });

function series(id: string, name: string, extra: Partial<Subscription> = {}): Subscription {
  return {
    id,
    account_id: "a-1",
    amount: money("-11.99"),
    amount_max: money("-11.99"),
    amount_min: money("-11.99"),
    cadence: "monthly",
    category_id: null,
    confidence: "0.8",
    first_seen_on: "2026-06-05",
    interval_count: 1,
    kind: "subscription",
    kind_source: "detector",
    last_seen_on: "2026-10-05",
    merchant: null,
    monthly_cost: money("11.99"),
    name,
    next_due: "2026-11-05",
    occurrences: 5,
    open_events: 0,
    origin: "detected",
    overdue: false,
    payee_id: null,
    price_change: null,
    status: "suggested",
    status_source: "detector",
    yearly_cost: money("143.88"),
    ...extra,
  };
}

const SUGGESTED = series("s-1", "Lumen Streaming");
const PRICE_UP: SubscriptionEvent = {
  id: "e-1",
  schedule_id: "s-2",
  name: "Inkwell Notes",
  kind: "price_changed",
  occurred_on: "2026-10-28",
  amount_before: money("-4.00"),
  amount_after: money("-5.00"),
  state: "open",
  transaction_id: "t-50",
};

/** Rows with a suggestion: two of Corner Market, one of Fuel Stop. */
const SUGGESTED_ROWS: Transaction[] = [
  tx("t-1", { category_id: null, merchant: "Corner Market", merchant_key: "corner market", suggested_category_id: "c-1" }),
  tx("t-2", { category_id: null, merchant: "Corner Market", merchant_key: "corner market", suggested_category_id: "c-1" }),
  tx("t-3", { category_id: null, merchant: "Fuel Stop", merchant_key: "fuel stop", suggested_category_id: "c-2" }),
];

const OFFER: LearnedCategory = {
  id: "m-1",
  category_id: "c-1",
  decisions: 3,
  total: 3,
  share: "1",
  direction: "out",
  example_transaction_id: "t-9",
  last_decided_at: NOW,
  merchant: "Corner Market",
  merchant_key: "corner market",
  offer_rule: true,
  rule_id: null,
};

interface World {
  role?: SessionOptions["role"];
  cards?: ReviewCard[];
  loose?: ReviewItem[];
  suggested?: Subscription[];
  events?: SubscriptionEvent[];
  rows?: Transaction[];
  suggestions?: number;
  uncategorised?: number;
  offers?: LearnedCategory[];
  documents?: DocumentRow[];
  extra?: Record<string, Handler>;
}

function inboxApi(world: World = {}) {
  const cards = world.cards ?? [];
  const loose = world.loose ?? [];
  const suggested = world.suggested ?? [];
  const events = world.events ?? [];
  const suggestions = world.suggestions ?? (world.rows ?? []).length;
  const series = new Set([...suggested.map((row) => row.id), ...events.map((one) => one.schedule_id)]).size;
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: world.role ?? "member" }),
    ...lookupHandlers([account("a-1", "Everyday")]),
    "GET /api/v1/documents": () => json({ items: world.documents ?? [], next_cursor: null }),
    "GET /api/v1/review/cards": () => json({ cards, loose, next_cursor: null }),
    "GET /api/v1/subscriptions/events": () => json({ items: events, next_cursor: null, open: events.length }),
    "GET /api/v1/subscriptions": () =>
      json({
        as_of: "2026-11-03",
        items: suggested,
        needs_look: suggested.length,
        bills: { count: 0, currency: "USD", monthly: money("0.00"), yearly: money("0.00"), unconverted: [] },
        subscriptions: { count: 0, currency: "USD", monthly: money("0.00"), yearly: money("0.00"), unconverted: [] },
      }),
    "GET /api/v1/categorisation/summary": () =>
      json(summary({ suggestions, uncategorised: world.uncategorised ?? suggestions })),
    "GET /api/v1/transactions": () => json({ items: world.rows ?? [], next_cursor: null }),
    "GET /api/v1/categorisation/learned": () => json({ items: world.offers ?? [], next_cursor: null }),
    "GET /api/v1/inbox/summary": () =>
      json({
        documents: cards.length,
        loose: loose.length,
        subscriptions: series,
        suggestions,
        uncategorised: world.uncategorised ?? suggestions,
        cards: cards.length + loose.length + series + (suggestions > 0 ? 1 : 0),
      }),
    ...world.extra,
  });
}

/** The day the Inbox tests of "Imported recently" run on. */
const TODAY = "2026-11-16T12:00:00Z";

/** A document in the library's list, finished `daysAgo` days before TODAY. */
function listed(id: string, name: string, status: DocumentRow["status"], daysAgo = 1, extra: Partial<DocumentRow> = {}): DocumentRow {
  const when = new Date(Date.parse(TODAY) - daysAgo * 86_400_000).toISOString();
  return {
    id,
    status,
    original_filename: name,
    kind: "statement",
    source: "upload",
    size: 10,
    mime_detected: "text/csv",
    sha256: "0".repeat(64),
    uploaded_by: null,
    account_id: null,
    institution_id: null,
    period_start: null,
    period_end: null,
    tax_year: null,
    parser_version: null,
    error: null,
    unchecked: false,
    created_at: when,
    updated_at: when,
    ...extra,
  };
}

function element(found: Element | null | undefined): HTMLElement {
  if (!(found instanceof HTMLElement)) {
    throw new Error("no element");
  }
  return found;
}

/** The cards that need an answer, in page order (their direct list items). */
async function waitingCards(): Promise<HTMLElement[]> {
  const list = await screen.findByRole("list", { name: "Waiting on you" });
  return within(list)
    .getAllByRole("listitem")
    .filter((entry) => entry.parentElement === list);
}

describe("Inbox, Needs you", () => {
  afterEach(() => {
    // "Not now" lasts for the browser session.
    window.sessionStorage.clear();
  });

  it("document cards come first in the spec's order", async () => {
    inboxApi({ cards: [NOTE_OLD, MONEY_NEW], loose: [LOOSE], suggested: [SUGGESTED] });
    renderApp("/inbox");
    expect(await screen.findByRole("link", { name: "maple-card-october.csv" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Lumen Streaming" })).toBeInTheDocument();
    const entries = await waitingCards();
    expect(entries).toHaveLength(4);
    // Money that is wrong until someone answers comes before a note, though the note is older.
    expect(within(element(entries[0])).getByRole("link", { name: "maple-card-october.csv" })).toBeInTheDocument();
    expect(within(element(entries[1])).getByRole("link", { name: "harbor-notes.csv" })).toBeInTheDocument();
    // Then the loose item, then the series.
    expect(within(element(entries[3])).getByRole("heading", { name: "Lumen Streaming" })).toBeInTheDocument();
  });

  it("a subscription price change can be acknowledged", async () => {
    const api = inboxApi({
      events: [PRICE_UP],
      extra: { "POST /api/v1/subscriptions/events/seen": () => json({ seen: 1 }) },
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const heading = await screen.findByRole("heading", { name: "Inkwell Notes" });
    const inkwell = heading.closest("section");
    expect(inkwell).not.toBeNull();
    expect(within(element(inkwell)).getByText(/Price went from \$4\.00 to \$5\.00/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Got it: Inkwell Notes" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/subscriptions/events/seen")[0]?.body).toEqual({ ids: ["e-1"] });
    });
  });

  it("a suggested series can be confirmed or dismissed", async () => {
    const other = series("s-3", "Pebble Cloud", { occurrences: 2 });
    const api = inboxApi({
      suggested: [SUGGESTED, other],
      extra: { "POST /api/v1/subscriptions/:id/decision": () => json(SUGGESTED) },
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    // A series PerFin found is a question until a person answers it.
    expect(await screen.findAllByText("Might be a subscription")).toHaveLength(2);
    await user.click(await screen.findByRole("button", { name: "Confirm Lumen Streaming" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/subscriptions/s-1/decision")[0]?.body).toEqual({ decision: "confirm" });
    });
    await user.click(screen.getByRole("button", { name: "Not a subscription: Pebble Cloud" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/subscriptions/s-3/decision")[0]?.body).toEqual({ decision: "dismiss" });
    });
  });

  it("suggestions are grouped by merchant and accepted per merchant", async () => {
    const api = inboxApi({
      rows: SUGGESTED_ROWS,
      uncategorised: 9,
      extra: { "POST /api/v1/transactions/bulk": () => json({ action: "accept_suggestions", changed: 2, results: [] }) },
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const panel = element((await screen.findByRole("heading", { name: "Suggested categories" })).closest("section"));
    // A suggestion reads as a question, never as a chosen category.
    expect(await within(panel).findByText("Groceries?")).toBeInTheDocument();
    expect(within(panel).getByText("Dining?")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Review all in Transactions" })).toHaveAttribute(
      "href",
      "/transactions?suggested=true",
    );
    expect(screen.getByRole("link", { name: "9 have no category" })).toHaveAttribute(
      "href",
      "/transactions?category=none",
    );
    await user.click(within(panel).getByRole("button", { name: "Accept 2 for Corner Market" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({
        action: "accept_suggestions",
        ids: ["t-1", "t-2"],
      });
    });
  });

  it("accepting never sends a row without a suggestion", async () => {
    const rows = [
      ...SUGGESTED_ROWS,
      tx("t-4", { category_id: null, merchant: "Corner Market", merchant_key: "corner market", suggested_category_id: null }),
      tx("t-5", { category_id: "c-2", merchant: "Corner Market", merchant_key: "corner market", suggested_category_id: "c-1" }),
    ];
    const api = inboxApi({
      rows,
      suggestions: 3,
      extra: { "POST /api/v1/transactions/bulk": () => json({ action: "accept_suggestions", changed: 2, results: [] }) },
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Accept 2 for Corner Market" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")).toHaveLength(1);
    });
    const body = api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body as { ids: string[] };
    expect(body.ids).not.toContain("t-4");
    expect(body.ids).not.toContain("t-5");
  });

  it("a rule offer links to the drawer", async () => {
    const api = inboxApi({ offers: [OFFER] });
    renderApp("/inbox");
    const user = userEvent.setup();
    const link = await screen.findByRole("link", { name: "Always use Groceries for Corner Market…" });
    expect(link).toHaveAttribute("href", "/transactions?always=t-9");
    // An offer is not a question: it is not among the cards that wait.
    expect(screen.queryByRole("list", { name: "Waiting on you" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not now: Corner Market" }));
    expect(screen.queryByRole("link", { name: "Always use Groceries for Corner Market…" })).not.toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/categorisation/learned/m-1")).toHaveLength(0);
  });

  it("a viewer sees the cards without buttons", async () => {
    inboxApi({
      role: "viewer",
      suggested: [SUGGESTED],
      events: [PRICE_UP],
      rows: SUGGESTED_ROWS,
      offers: [OFFER],
    });
    renderApp("/inbox");
    expect(await screen.findByRole("heading", { name: "Lumen Streaming" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Inkwell Notes" })).toBeInTheDocument();
    expect(await screen.findByText("Groceries?")).toBeInTheDocument();
    const offer = await screen.findByRole("region", { name: "Always Groceries for Corner Market?" });
    expect(within(offer).getByText(/You chose Groceries for Corner Market 3 of 3 times/)).toBeInTheDocument();
    for (const name of [/^Confirm/, /^Not a subscription/, /^Got it/, /^Accept/, /^Not now/]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("link", { name: /^Always use/ })).not.toBeInTheDocument();
  });

  it("the number of cards that need an answer equals the summary's cards", async () => {
    const changed = series("s-2", "Inkwell Notes", { open_events: 1 });
    const api = inboxApi({
      cards: [NOTE_OLD, MONEY_NEW],
      loose: [LOOSE],
      // A suggested series with an open change is one card.
      suggested: [SUGGESTED, changed],
      events: [PRICE_UP],
      rows: SUGGESTED_ROWS,
      offers: [OFFER],
    });
    renderApp("/inbox");
    expect(await screen.findByRole("heading", { name: "Suggested categories" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Inkwell Notes" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Always use Groceries for Corner Market…" })).toBeInTheDocument();
    const entries = await waitingCards();
    // 2 documents + 1 loose + 2 series + 1 suggestions card; the rule offer is not counted.
    expect(entries).toHaveLength(6);
    expect(await screen.findByRole("link", { name: "Needs you, 6" })).toBeInTheDocument();
    // The cards read what the summary counts.
    const query = (path: string) => new URLSearchParams(api.callsTo("GET", path)[0]?.search ?? "");
    expect(query("/api/v1/subscriptions").getAll("status")).toEqual(["suggested"]);
    expect(query("/api/v1/subscriptions/events").get("state")).toBe("open");
    expect(query("/api/v1/transactions").get("suggested")).toBe("true");
    expect(query("/api/v1/transactions").get("limit")).toBe("200");
  });

  it("the badge, Home's needs-you and the Inbox show the same number", async () => {
    // One summary and one card set, for a viewer: the server counts only what the viewer may see,
    // so the three places must agree with it and with each other. Home shows no figure, only up
    // to three rows, so the set holds three cards (a document, a loose item and a series) and no
    // suggestions, whose card Home shows as the uncategorised line instead.
    inboxApi({ role: "viewer", cards: [MONEY_NEW], loose: [LOOSE], suggested: [SUGGESTED] });
    renderApp("/");
    const user = userEvent.setup();
    const [rail] = await screen.findAllByRole("link", { name: "Inbox, 3 things need you" });
    const panel = await screen.findByRole("region", { name: "Needs you" });
    const list = await within(panel).findByRole("list", { name: "What needs you" });
    await within(list).findByText("Lumen Streaming");
    const homeRows = within(list).getAllByRole("link");
    expect(homeRows).toHaveLength(3);
    const hrefs = homeRows.map((row) => row.getAttribute("href") ?? "");
    // Each row links to its own card: a document by `?document=`, the others by their card's anchor.
    expect(hrefs).toEqual(["/inbox?document=d-2", "/inbox#inbox-item-r-loose", "/inbox#inbox-series-s-1"]);

    await user.click(element(rail));
    expect(await screen.findByRole("link", { name: "Needs you, 3" })).toBeInTheDocument();
    const entries = await waitingCards();
    await within(element(entries[2])).findByRole("heading", { name: "Lumen Streaming" });
    expect(entries).toHaveLength(3);
    // The same cards in the same order as Home's rows, every one of them.
    const cardOf = (href: string): string => {
      const url = new URL(href, "http://perfin.test");
      const documentId = url.searchParams.get("document");
      return documentId === null ? url.hash.slice(1) : `inbox-document-${documentId}`;
    };
    expect(entries.map((entry) => entry.id)).toEqual(hrefs.map(cardOf));
    expect(within(element(entries[0])).getByRole("link", { name: "maple-card-october.csv" })).toBeInTheDocument();
  });

  it.each([
    ["Lumen Streaming", /^Lumen Streaming/, "inbox-series-s-1"],
    ["An item of no document", /^An item of no document/, "inbox-item-r-loose"],
  ])("Home's %s row opens the Inbox at its card", async (_title, name, anchor) => {
    // jsdom has no scrollIntoView; the router and the Inbox call it in a browser.
    const scrolled: string[] = [];
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value(this: Element) {
        scrolled.push(this.id);
      },
    });
    onTestFinished(() => {
      Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    });
    inboxApi({ cards: [MONEY_NEW], loose: [LOOSE], suggested: [SUGGESTED] });
    renderApp("/");
    const user = userEvent.setup();
    const panel = await screen.findByRole("region", { name: "Needs you" });
    const list = await within(panel).findByRole("list", { name: "What needs you" });
    // The series row comes last, once every row has loaded.
    await within(list).findByText("Lumen Streaming");
    await user.click(within(list).getByRole("link", { name }));
    // The card is focused once it has loaded, so a person lands on it, not at the top of the Inbox.
    await waitFor(() => {
      expect(window.document.activeElement?.id).toBe(anchor);
    });
    expect(window.document.activeElement?.closest("ul")).toHaveAccessibleName("Waiting on you");
    expect(scrolled).toContain(anchor);
  });

  it("?document= shows one card", async () => {
    inboxApi({
      cards: [NOTE_OLD, MONEY_NEW],
      loose: [LOOSE],
      suggested: [SUGGESTED],
      rows: SUGGESTED_ROWS,
      offers: [OFFER],
    });
    renderApp("/inbox?document=d-1");
    expect(await screen.findByRole("link", { name: "harbor-notes.csv" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "maple-card-october.csv" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Lumen Streaming" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Suggested categories" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^Always use/ })).not.toBeInTheDocument();
    expect(await waitingCards()).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Show everything" })).toHaveAttribute("href", "/inbox");
  });

  it("the empty state", async () => {
    inboxApi();
    renderApp("/inbox");
    expect(await screen.findByRole("heading", { name: "All clear" })).toBeInTheDocument();
    expect(
      screen.getByText("Every document you added is imported. Drop new statements below any time."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Waiting on you" })).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    inboxApi({
      cards: [NOTE_OLD, MONEY_NEW],
      loose: [LOOSE],
      suggested: [SUGGESTED],
      events: [PRICE_UP],
      rows: SUGGESTED_ROWS,
      uncategorised: 9,
      offers: [OFFER],
    });
    const { container } = renderApp("/inbox");
    await screen.findByRole("heading", { name: "Suggested categories" });
    await screen.findByRole("heading", { name: "Inkwell Notes" });
    await screen.findByRole("link", { name: "Always use Groceries for Corner Market…" });
    // The whole page: the upload zone, the cards and what was imported recently.
    await expectNoAxeViolations(container);
  });
});

describe("Inbox, imported recently", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(TODAY));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("recently imported names the account and marks unchecked reads", async () => {
    inboxApi({
      documents: [
        listed("d-10", "everyday-october.csv", "processed", 1, {
          account_id: "a-1",
          period_start: "2026-10-01",
          period_end: "2026-10-31",
          unchecked: true,
        }),
        // An account the person cannot see is not named; the rows still are linked.
        listed("d-11", "joint-october.pdf", "processed", 2, { account_id: "a-9" }),
        // Investment activity is not in the cash ledger: no rows to link.
        listed("d-12", "trades.csv", "processed", 3, { parser_version: "brokerage_activity_table/1" }),
        // Holdings are not either; they say so in their own words.
        listed("d-16", "holdings.csv", "processed", 3, { parser_version: "brokerage_positions_table/1" }),
        // A receipt is not a statement: it has no rows in the ledger.
        listed("d-17", "till.jpg", "processed", 4, { kind: "receipt" }),
        // A file that came without a name still has a row, and a link.
        listed("d-18", "", "processed", 5, { original_filename: null }),
        // Still being read, waiting on a person, or older than two weeks: not here.
        listed("d-13", "reading.pdf", "parsing", 0),
        listed("d-14", "held.csv", "needs_review", 1, { account_id: "a-1" }),
        listed("d-15", "old.csv", "processed", 20),
      ],
    });
    renderApp("/inbox");
    const recent = within(await screen.findByRole("region", { name: "Imported recently" }));
    expect(await recent.findByRole("link", { name: "everyday-october.csv" })).toHaveAttribute(
      "href",
      "/inbox/documents/d-10",
    );
    expect(recent.getByRole("link", { name: "Everyday" })).toHaveAttribute("href", "/accounts/a-1");
    expect(recent.getAllByRole("link", { name: "Everyday" })).toHaveLength(1);
    expect(recent.getByText(`${String(formatDate("2026-10-01"))} to ${String(formatDate("2026-10-31"))}`)).toBeInTheDocument();
    expect(recent.getAllByText("Not checked against a balance")).toHaveLength(1);
    expect(recent.getByText("Read as investment activity")).toBeInTheDocument();
    expect(recent.getByText("Read as investment holdings")).toBeInTheDocument();
    expect(recent.getByRole("link", { name: "till.jpg" })).toHaveAttribute("href", "/inbox/documents/d-17");
    expect(recent.getByRole("link", { name: "Unnamed file" })).toHaveAttribute("href", "/inbox/documents/d-18");
    expect(
      recent.getAllByRole("link", { name: /^View the rows/ }).map((link) => link.getAttribute("href")),
    ).toEqual(["/transactions?document=d-10", "/transactions?document=d-11", "/transactions?document=d-18"]);
    for (const name of ["reading.pdf", "held.csv", "old.csv"]) {
      expect(recent.queryByText(name)).not.toBeInTheDocument();
    }
  });

  it("a failed file says so", async () => {
    inboxApi({ documents: [listed("d-20", "blurry-scan.png", "failed", 1)] });
    renderApp("/inbox");
    const recent = within(await screen.findByRole("region", { name: "Imported recently" }));
    expect(await recent.findByRole("link", { name: "blurry-scan.png" })).toHaveAttribute("href", "/inbox/documents/d-20");
    expect(recent.getByText("Could not be read")).toBeInTheDocument();
    expect(recent.queryByRole("link", { name: /^View the rows/ })).not.toBeInTheDocument();
  });

  it("keeps the last ten finished in two weeks, newest first", () => {
    const now = new Date(TODAY);
    const rows = [
      ...Array.from({ length: 12 }, (_, day) => listed(`d-${String(day)}`, `file-${String(day)}.csv`, "processed", day)),
      listed("d-x", "rejected.csv", "rejected", 0),
      listed("d-y", "edge.csv", "failed", 14.5),
    ];
    const kept = recentImports(rows, now);
    expect(kept.map((row) => row.id)).toEqual(Array.from({ length: 10 }, (_, day) => `d-${String(day)}`));
    expect(recentImports([listed("d-y", "edge.csv", "failed", 13.9)], now).map((row) => row.id)).toEqual(["d-y"]);
  });
});
