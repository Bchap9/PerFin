// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, noContent, problem, type Handler } from "../auth/testing";
import { NOW, tx } from "../transactions/fixtures";
import type { ReconcileRow, StatementReconciliation } from "./api";
import { ReconcileView, type ReconcileViewProps } from "./ReconcileView";

function row(
  id: string,
  payee: string,
  amount: string,
  extra: Partial<ReconcileRow> = {},
): ReconcileRow {
  return {
    transaction_id: id,
    posted_date: "2026-10-09",
    payee,
    amount,
    origin: "read",
    edited: false,
    in_period: true,
    review_state: "pending",
    page: 1,
    bbox: [40, 200, 500, 214],
    ...extra,
  };
}

const ROWS: ReconcileRow[] = [
  row("t-1", "Harbour Grocer", "-40.00"),
  row("t-2", "Tram pass", "-25.50"),
  row("t-3", "Closing balance", "-12.00"),
];

function statement(
  extra: Partial<StatementReconciliation> = {},
): StatementReconciliation {
  return {
    statement: 0,
    account_id: "a-1",
    account_name: "Everyday",
    can_edit: true,
    causes: [
      {
        kind: "extra_row",
        amount: "-12.00",
        page: 1,
        bbox: [40, 240, 500, 254],
        transaction_ids: ["t-3"],
      },
    ],
    opening: "500.00",
    closing: "434.50",
    previous_closing: null,
    currency: "USD",
    total: "-77.50",
    difference: "-12.00",
    lines_skipped: 0,
    period_start: "2026-10-01",
    period_end: "2026-10-31",
    printed_sign: "ledger",
    removed: [],
    rows: ROWS,
    rows_truncated: false,
    status: "off",
    verification: "unverified",
    ...extra,
  };
}

const BALANCED = statement({
  causes: [],
  rows: ROWS.slice(0, 2),
  total: "-65.50",
  difference: "0.00",
  status: "balanced",
  removed: [
    {
      tombstone_id: "x-1",
      amount: "-12.00",
      posted_date: "2026-10-09",
      deleted_at: NOW,
    },
  ],
});

const PAGE = {
  document_id: "d-1",
  page: 1,
  page_count: 1,
  width: 612,
  height: 792,
  unit: "pt",
  preview: null,
  text_included: true,
  text_source: "text_layer",
  words: [],
  words_truncated: false,
};

function outcome(after: StatementReconciliation) {
  return json({ confirmed: 2, items_closed: 1, snapshot: true, statement: after });
}

function setup(
  first: StatementReconciliation = statement(),
  extra: Record<string, Handler> = {},
  props: Partial<ReconcileViewProps> = {},
) {
  const state = { current: first };
  const api = mockApi({
    "GET /api/v1/documents/:id/reconciliation": () =>
      json({ document_id: "d-1", statements: [state.current] }),
    "GET /api/v1/documents/:id/regions": () => json(PAGE),
    ...extra,
  });
  const onDone = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ReconcileView documentId="d-1" statement={0} onDone={onDone} {...props} />
    </QueryClientProvider>,
  );
  return { api, onDone, state, user: userEvent.setup() };
}

function writes(api: ReturnType<typeof mockApi>) {
  return api.calls.filter((call) => call.method !== "GET");
}

async function tableRow(name: string): Promise<HTMLElement> {
  const cells = await screen.findAllByRole("rowheader");
  const found = cells
    .find((cell) => cell.textContent.includes(name))
    ?.closest("tr");
  if (found === null || found === undefined) {
    throw new Error(`no row for ${name}`);
  }
  return found;
}

describe("ReconcileView", () => {
  it("shows opening, closing, the rows' total and the difference", async () => {
    setup();
    expect(
      await screen.findByRole("heading", { name: "Check against the statement" }),
    ).toBeVisible();
    expect(screen.getByLabelText("Opening balance")).toHaveValue("500.00");
    expect(screen.getByLabelText("Closing balance")).toHaveValue("434.50");
    const figures = screen.getByRole("group", { name: "The statement's figures" });
    expect(within(figures).getByText("Rows in PerFin")).toBeVisible();
    expect(within(figures).getByText("-$77.50")).toBeVisible();
    expect(within(figures).getByText("Opening + rows")).toBeVisible();
    expect(within(figures).getByText("$422.50")).toBeVisible();
    const live = screen.getByTestId("reconcile-difference");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("Difference");
    expect(live).toHaveTextContent("-$12.00");
    expect(live).toHaveTextContent("Off by $12.00.");
  });

  it("recomputes the difference while a balance is typed", async () => {
    const { user } = setup();
    const closing = await screen.findByLabelText("Closing balance");
    await user.clear(closing);
    expect(screen.getByTestId("reconcile-difference")).toHaveTextContent(
      "No balances to check against. Enter them as printed on the statement.",
    );
    await user.type(closing, "422.50");
    expect(screen.getByTestId("reconcile-difference")).toHaveTextContent("Balanced.");
  });

  it("marks the row the first cause names", async () => {
    setup();
    const flagged = await tableRow("Closing balance");
    expect(flagged).toHaveClass("reconcile__row--flagged");
    expect(within(flagged).getByText("Check this row")).toBeVisible();
    expect(await tableRow("Harbour Grocer")).not.toHaveClass("reconcile__row--flagged");
    expect(
      screen.getByText(
        "One row is exactly the difference. It may be a row too many, such as a total or a balance line read as a transaction.",
      ),
    ).toBeVisible();
  });

  it("confirm is disabled until the difference is zero and then posts the typed balances", async () => {
    const { api, onDone, user } = setup(statement(), {
      "POST /api/v1/documents/:id/reconciliation/confirm": () => outcome(BALANCED),
    });
    const confirm = await screen.findByRole("button", {
      name: "Confirm and record the balance",
    });
    expect(confirm).toBeDisabled();
    const closing = screen.getByLabelText("Closing balance");
    await user.clear(closing);
    await user.type(closing, "422.50");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const [call] = api.callsTo("POST", "/api/v1/documents/d-1/reconciliation/confirm");
    expect(call?.body).toEqual({
      statement: 0,
      opening_balance: "500.00",
      closing_balance: "422.50",
    });
  });

  it("removing a row deletes the transaction and reloads the sum", async () => {
    const { api, state, user } = setup(statement(), {
      "DELETE /api/v1/transactions/:id": () => {
        state.current = BALANCED;
        return noContent();
      },
    });
    const flagged = await tableRow("Closing balance");
    await user.click(within(flagged).getByRole("button", { name: /^Remove/ }));
    await waitFor(() => {
      expect(screen.getByTestId("reconcile-difference")).toHaveTextContent("Balanced.");
    });
    expect(api.callsTo("DELETE", "/api/v1/transactions/t-3")).toHaveLength(1);
    expect(
      api.callsTo("GET", "/api/v1/documents/d-1/reconciliation").length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("rowheader", { name: /Closing balance/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Confirm and record the balance" })).toBeEnabled();
  });

  it("flip sign patches the amount with the opposite sign", async () => {
    const { api, user } = setup(statement(), {
      "PATCH /api/v1/transactions/:id": () => json(tx("t-3")),
    });
    const flagged = await tableRow("Closing balance");
    await user.click(within(flagged).getByRole("button", { name: /^Flip sign/ }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-3")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/transactions/t-3")[0]?.body).toEqual({
      amount: { amount: "12.00", currency: "USD" },
    });
  });

  it("edit patches the date and the amount of the row", async () => {
    const { api, user } = setup(statement(), {
      "PATCH /api/v1/transactions/:id": () => json(tx("t-2")),
    });
    const target = await tableRow("Tram pass");
    await user.click(within(target).getByRole("button", { name: /^Edit/ }));
    const amount = screen.getByLabelText("Amount of the row");
    await user.clear(amount);
    await user.type(amount, "-52.50");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-2")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/transactions/t-2")[0]?.body).toEqual({
      posted_date: "2026-10-09",
      amount: { amount: "-52.50", currency: "USD" },
    });
  });

  it("add a row posts a transaction with the document as its source", async () => {
    const { api, user } = setup(statement(), {
      "POST /api/v1/transactions": () => json(tx("t-9"), 201),
    });
    await user.click(await screen.findByRole("button", { name: "Add a row" }));
    await user.type(screen.getByLabelText(/^Date/), "2026-10-30");
    await user.type(screen.getByLabelText(/^Amount/), "-3.00");
    await user.type(screen.getByLabelText("Description"), "Account fee");
    await user.click(screen.getByRole("button", { name: "Add the row" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(1);
    });
    const [call] = api.callsTo("POST", "/api/v1/transactions");
    expect(call?.body).toEqual({
      account_id: "a-1",
      posted_date: "2026-10-30",
      amount: { amount: "-3.00", currency: "USD" },
      raw_payee: "Account fee",
      status: "cleared",
      source_document_id: "d-1",
    });
    expect(call?.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("accept asks first and posts accept", async () => {
    const { api, onDone, user } = setup(statement(), {
      "POST /api/v1/documents/:id/reconciliation/accept": () => outcome(statement()),
    });
    await user.click(await screen.findByRole("button", { name: "Accept as read" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(
      "Accept this statement as read? Its rows are posted and it is marked “accepted without balancing”. No balance is recorded.",
    );
    expect(writes(api)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Accept as read" }));
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(
      api.callsTo("POST", "/api/v1/documents/d-1/reconciliation/accept")[0]?.body,
    ).toEqual({ statement: 0 });
  });

  it("does not offer to accept a statement that balances", async () => {
    const { user } = setup();
    const accept = await screen.findByRole("button", { name: "Accept as read" });
    expect(accept).toBeEnabled();
    const closing = screen.getByLabelText("Closing balance");
    await user.clear(closing);
    await user.type(closing, "422.50");
    expect(screen.getByTestId("reconcile-difference")).toHaveTextContent("Balanced.");
    // A statement that balances is confirmed, with its balance recorded.
    expect(accept).toBeDisabled();
  });

  it("says in the question why an acceptance was refused", async () => {
    const { onDone, user } = setup(statement(), {
      "POST /api/v1/documents/:id/reconciliation/accept": () =>
        problem(409, "statement-unavailable"),
    });
    await user.click(await screen.findByRole("button", { name: "Accept as read" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Accept as read" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "detail of statement-unavailable",
    );
    expect(onDone).not.toHaveBeenCalled();
  });

  it("finish later calls onDone without a request", async () => {
    const { api, onDone, user } = setup();
    await user.click(await screen.findByRole("button", { name: "Finish later" }));
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(writes(api)).toHaveLength(0);
  });

  it("a removed row can be put back", async () => {
    const { api, state, user } = setup(BALANCED, {
      "POST /api/v1/documents/:id/reconciliation/restore": () => {
        state.current = statement();
        return outcome(statement());
      },
    });
    const removed = await screen.findByRole("list", { name: "Removed rows" });
    await user.click(within(removed).getByRole("button", { name: /^Put back/ }));
    await waitFor(() => {
      expect(screen.getByRole("rowheader", { name: /Closing balance/ })).toBeVisible();
    });
    expect(
      api.callsTo("POST", "/api/v1/documents/d-1/reconciliation/restore")[0]?.body,
    ).toEqual({ tombstone_id: "x-1" });
  });

  it("removes the rows of an earlier reading in one action", async () => {
    const earlier = statement({
      causes: [
        {
          kind: "earlier_reading",
          amount: null,
          page: null,
          bbox: null,
          transaction_ids: ["t-3"],
        },
      ],
    });
    const { api, user } = setup(earlier, {
      "POST /api/v1/documents/:id/reconciliation/remove-earlier": () => outcome(BALANCED),
    });
    await user.click(
      await screen.findByRole("button", {
        name: "Remove the rows of the earlier reading",
      }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/documents/d-1/reconciliation/remove-earlier"),
      ).toHaveLength(1);
    });
    expect(
      api.callsTo("POST", "/api/v1/documents/d-1/reconciliation/remove-earlier")[0]?.body,
    ).toEqual({ statement: 0 });
  });

  it("choosing a row shows its box on the page", async () => {
    const { user } = setup();
    const target = await tableRow("Tram pass");
    expect(screen.queryByTestId("region-box")).toBeNull();
    await user.click(within(target).getByRole("button", { name: /^Show on the page/ }));
    expect(await screen.findByTestId("region-box")).toBeInTheDocument();
  });

  it("a cause's button goes to the row it names and shows its box on the page", async () => {
    const { user } = setup();
    const flagged = await tableRow("Closing balance");
    expect(flagged).not.toHaveClass("reconcile__row--chosen");
    expect(screen.queryByTestId("region-box")).toBeNull();
    // jsdom has no layout: scrolling is stood in for, to see that it is asked.
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    try {
      await user.click(screen.getByRole("button", { name: "Show the row" }));
    } finally {
      Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    }
    expect(flagged).toHaveClass("reconcile__row--chosen");
    expect(await tableRow("Tram pass")).not.toHaveClass("reconcile__row--chosen");
    expect(scrolled).toHaveBeenCalledTimes(1);
    expect(flagged).toHaveFocus();
    expect(await screen.findByTestId("region-box")).toBeInTheDocument();
  });

  it("opens the statement still in doubt when none is named", async () => {
    const settled = statement({
      causes: [],
      difference: "0.00",
      status: "balanced",
      verification: "reconciled",
    });
    const doubted = statement({
      statement: 1,
      account_id: "a-2",
      account_name: "Savings",
    });
    setup(
      settled,
      {
        "GET /api/v1/documents/:id/reconciliation": () =>
          json({ document_id: "d-1", statements: [settled, doubted] }),
      },
      { statement: null },
    );
    expect(await screen.findByText(/^Savings,/)).toBeVisible();
    expect(screen.queryByText(/^Everyday,/)).toBeNull();
  });

  it("suggests teaching the layout after three rows were removed", async () => {
    const onTeach = vi.fn();
    const { user } = setup(
      statement(),
      { "DELETE /api/v1/transactions/:id": () => noContent() },
      { onTeach },
    );
    for (const name of ["Closing balance", "Tram pass", "Harbour Grocer"]) {
      const target = await tableRow(name);
      await user.click(within(target).getByRole("button", { name: /^Remove/ }));
      await waitFor(() => {
        expect(
          within(target).getByRole("button", { name: /^Remove/ }),
        ).toBeEnabled();
      });
    }
    expect(
      await screen.findByText(
        "PerFin read several rows of this layout wrongly. Teach it the layout so the next statement reads right.",
      ),
    ).toBeVisible();
    // The hint names an action, so the action is offered beside it.
    await user.click(screen.getByRole("button", { name: "Teach PerFin" }));
    expect(onTeach).toHaveBeenCalledTimes(1);
  });

  it("offers no teach button where teaching cannot be opened", async () => {
    const { user } = setup(statement(), {
      "DELETE /api/v1/transactions/:id": () => noContent(),
    });
    for (const name of ["Closing balance", "Tram pass", "Harbour Grocer"]) {
      const target = await tableRow(name);
      await user.click(within(target).getByRole("button", { name: /^Remove/ }));
      await waitFor(() => {
        expect(
          within(target).getByRole("button", { name: /^Remove/ }),
        ).toBeEnabled();
      });
    }
    await screen.findByText(/^PerFin read several rows of this layout wrongly/);
    expect(screen.queryByRole("button", { name: "Teach PerFin" })).toBeNull();
  });

  it("a viewer sees the figures and no actions", async () => {
    const { api } = setup(
      statement({ can_edit: false, removed: BALANCED.removed }),
    );
    expect(
      await screen.findByRole("heading", { name: "Check against the statement" }),
    ).toBeVisible();
    const figures = screen.getByRole("group", { name: "The statement's figures" });
    expect(within(figures).getByText("$500.00")).toBeVisible();
    expect(within(figures).getByText("$434.50")).toBeVisible();
    expect(screen.getByTestId("reconcile-difference")).toHaveTextContent("Off by $12.00.");
    expect(screen.queryByLabelText("Opening balance")).toBeNull();
    for (const name of [
      /^Confirm and record/,
      /^Accept as read/,
      /^Finish later/,
      /^Edit/,
      /^Flip sign/,
      /^Remove/,
      /^Add a row/,
      /^Put back/,
    ]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.getByRole("button", { name: "Close" })).toBeVisible();
    expect(writes(api)).toHaveLength(0);
  });

  it("shows the card hint for a card", async () => {
    setup(
      statement({
        printed_sign: "owed",
        opening: "100.00",
        closing: "150.00",
        total: "-50.00",
        difference: "0.00",
        status: "balanced",
        causes: [],
      }),
    );
    expect(
      await screen.findByText(
        "Enter the balances as printed. For a card, the amount owed is a positive number.",
      ),
    ).toBeVisible();
    expect(screen.getByTestId("reconcile-difference")).toHaveTextContent("Balanced.");
  });

  it("does not show the card hint for a bank account", async () => {
    setup();
    await screen.findByRole("heading", { name: "Check against the statement" });
    expect(screen.queryByText(/For a card, the amount owed/)).toBeNull();
  });

  it("says how many lines were not read", async () => {
    // No box is drawn for the lines, so the page is not said to mark them.
    setup(statement({ lines_skipped: 2 }));
    expect(
      await screen.findByText("2 lines of the statement were not read."),
    ).toBeVisible();
    expect(screen.queryByText(/marked on the page/)).toBeNull();
  });

  it("says the skipped lines are marked when their boxes are drawn", async () => {
    setup(
      statement({ lines_skipped: 2 }),
      {},
      {
        regions: [
          { page: 1, bbox: [40, 300, 500, 314], label: "skipped_line" },
          { page: 1, bbox: [40, 320, 500, 334], label: "skipped_line" },
        ],
      },
    );
    expect(
      await screen.findByText(
        "2 lines of the statement were not read. They are marked on the page.",
      ),
    ).toBeVisible();
    expect(await screen.findAllByTestId("region-box")).toHaveLength(2);
  });

  it("says the total counts every row when only the first are listed", async () => {
    setup(statement({ rows_truncated: true }));
    expect(
      await screen.findByText(
        "Only the first rows are listed. The total counts every row.",
      ),
    ).toBeVisible();
  });
});
