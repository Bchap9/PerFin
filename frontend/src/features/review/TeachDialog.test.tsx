// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { account, lookupHandlers, NOW } from "../transactions/fixtures";
import type { ReviewItem } from "./api";
import {
  DIRECTION_CHOICES,
  IGNORE_LABEL,
  OTHER_LABEL,
  SELECTION_CHOICES,
  columnsFromWords,
  keptText,
  newSelection,
  previewSummary,
  problems,
  rolesForTable,
  teachBody,
  warningText,
  type PageWord,
  type Selection,
  type TeachRole,
} from "./teach";

const WORDS: PageWord[] = [
  { bbox: [60, 40, 110, 50], line: 0, text: "Carried" },
  { bbox: [114, 40, 130, 50], line: 0, text: "out" },
  { bbox: [140, 40, 190, 50], line: 0, text: "921.93" },
  { bbox: [60, 80, 100, 90], line: 1, text: "Booked" },
  { bbox: [150, 80, 210, 90], line: 1, text: "Narrative" },
  { bbox: [400, 80, 450, 90], line: 1, text: "Movement" },
  { bbox: [60, 100, 120, 110], line: 2, text: "2026-04-07" },
  { bbox: [150, 100, 200, 110], line: 2, text: "Grocer" },
  { bbox: [410, 100, 450, 110], line: 2, text: "-62.57" },
];

function item(extra: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: "r-1",
    kind: "unparsed",
    state: "open",
    document_id: "d-1",
    document: {
      id: "d-1",
      original_filename: "ledger.pdf",
      kind: "unknown",
      status: "needs_review",
      mime_detected: "application/pdf",
      source: "upload",
      created_at: NOW,
      period_start: null,
      period_end: null,
      previewable: true,
    },
    reason: "no_records",
    payload: { reason: "no_records" },
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: "reprocess",
    can_resolve: true,
    ...extra,
  };
}

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
  words: WORDS.map((w) => ({ ...w, conf: null })),
  words_truncated: false,
};

/** What a preview answers: nothing is saved, the reading is shown. */
function previewOut(extra: Record<string, unknown> = {}) {
  return {
    document_id: "d-1",
    fields: [],
    tables: [],
    ignored: 0,
    notes: [],
    applied: [],
    warnings: [],
    reprocessed: [],
    preview: {
      rows: [],
      row_count: 0,
      reconciliation: "unavailable",
      difference: null,
      similar_waiting: 0,
      account_id: null,
      ...extra,
    },
  };
}

function appliedOut(extra: Record<string, unknown> = {}) {
  return {
    document_id: "d-1",
    fields: [],
    tables: [],
    ignored: 0,
    notes: [],
    applied: [],
    warnings: [],
    reprocessed: [],
    ...extra,
  };
}

function isPreview(body: unknown): boolean {
  return typeof body === "object" && body !== null && "preview" in body && body.preview === true;
}

/** Answers a preview with ``shown`` and an apply with ``applied``. */
function teachHandler(shown: unknown, applied: unknown = appliedOut()): Handler {
  return (call) => json(isPreview(call.body) ? shown : applied);
}

/** The action the server names for a card of this one item. */
function cardAction(detail: ReviewItem): string {
  if (detail.kind === "unparsed") {
    return "reprocess";
  }
  return detail.reason === "reconciliation_failed" ? "reconcile" : "acknowledge";
}

function reviewApi(
  detail: ReviewItem,
  extra: Record<string, Handler> = {},
  documentAccount: string | null = null,
) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner" }),
    ...lookupHandlers([account("a-1", "Everyday"), account("a-2", "Rainy day")]),
    "GET /api/v1/documents/:id": () => json({ id: "d-1", account_id: documentAccount }),
    "GET /api/v1/review": () => json({ items: [detail], next_cursor: null }),
    // The queue as cards by document: the one item and the action it asks for.
    "GET /api/v1/review/cards": () =>
      json({
        cards: [
          {
            document: detail.document,
            items: [detail],
            action: cardAction(detail),
            action_item_id: detail.id,
            open: 1,
            notices: 0,
          },
        ],
        loose: [],
        next_cursor: null,
      }),
    "GET /api/v1/review/:id": () => json(detail),
    "GET /api/v1/documents/:id/regions": () => json(PAGE),
    ...extra,
  });
}

describe("teach helpers", () => {
  it("always offers Other and Ignore, for selections and for every column", () => {
    const labels = SELECTION_CHOICES.map((c) => c.label);
    expect(labels).toContain(OTHER_LABEL);
    expect(labels).toContain(IGNORE_LABEL);
    for (const kind of ["transactions", "holdings"] as const) {
      const roles = rolesForTable(kind).map((r) => r.label);
      expect(roles).toContain(OTHER_LABEL);
      expect(roles).toContain(IGNORE_LABEL);
    }
    expect(rolesForTable("transactions").map((r) => r.value)).toContain("debit");
    expect(rolesForTable("holdings").map((r) => r.value)).toContain("market_value");
  });

  it("splits a selected table into columns from its header line", () => {
    const columns = columnsFromWords(WORDS, [50, 75, 500, 115]);
    expect(columns.map((c) => c.header)).toEqual(["Booked", "Narrative", "Movement"]);
    expect(columns[0]?.x0).toBe(50);
    expect(columns[2]?.x1).toBe(500);
    expect(columns[0]?.x1).toBe(125);
  });

  it("builds the request and lists what is missing", () => {
    const table = { ...newSelection(1, [50, 75, 500, 115], WORDS), kind: "transactions" as const };
    expect(problems([table])).toHaveLength(3);
    const roles: readonly TeachRole[] = ["date", "other", "amount"];
    const ready = {
      ...table,
      columns: table.columns.map((c, i) => ({
        ...c,
        role: roles.at(i) ?? "ignore",
        note: i === 1 ? "Who was paid" : "",
      })),
    };
    expect(problems([ready])).toEqual([]);
    const body = teachBody([ready], {
      institutionName: " Finch Society ",
      remember: true,
      reprocessSimilar: false,
      itemId: null,
      accountId: null,
      preview: false,
    });
    expect(body).toEqual({
      labels: [],
      tables: [
        {
          kind: "transactions",
          page: 1,
          bbox: [50, 75, 500, 115],
          columns: [
            { x0: 50, x1: 125, role: "date" },
            { x0: 125, x1: 305, role: "other", note: "Who was paid" },
            { x0: 305, x1: 500, role: "amount" },
          ],
          direction: "as_printed",
          continues: false,
        },
      ],
      remember: true,
      reprocess_similar: false,
      replace: false,
      resolve: false,
      preview: false,
      institution_name: "Finch Society",
    });
  });

  function table(kind: "transactions" | "holdings", extra: Partial<Selection> = {}): Selection {
    const drawn = newSelection(1, [50, 75, 500, 115], WORDS);
    const roles: readonly TeachRole[] =
      kind === "transactions" ? ["date", "payee", "amount"] : ["symbol", "quantity", "price"];
    return {
      ...drawn,
      kind,
      columns: drawn.columns.map((c, i) => ({ ...c, role: roles.at(i) ?? "ignore" })),
      ...extra,
    };
  }

  it("teachBody carries the direction, the continuation and the account", () => {
    const drawn = newSelection(1, [50, 75, 500, 115], WORDS);
    expect(drawn.direction).toBe("as_printed");
    expect(drawn.continues).toBe(false);
    const body = teachBody(
      [table("transactions", { direction: "money_out", continues: true }), table("holdings")],
      {
        institutionName: "",
        remember: false,
        reprocessSimilar: true,
        itemId: "r-1",
        accountId: "a-2",
        preview: true,
      },
    );
    expect(body.account_id).toBe("a-2");
    expect(body.preview).toBe(true);
    expect(body.tables?.[0]).toMatchObject({
      kind: "transactions",
      direction: "money_out",
      continues: true,
    });
    // A holdings table has no direction to say.
    expect(body.tables?.[1]).not.toHaveProperty("direction");
    expect(body.tables?.[1]).not.toHaveProperty("continues");
    const none = teachBody([], {
      institutionName: "",
      remember: false,
      reprocessSimilar: true,
      itemId: null,
      accountId: null,
      preview: false,
    });
    expect(none).not.toHaveProperty("account_id");
    expect(none.preview).toBe(false);
  });

  it("offers the four directions in plain words", () => {
    expect(DIRECTION_CHOICES).toEqual([
      { value: "as_printed", label: "As printed (the amounts carry their own sign)" },
      { value: "money_in", label: "Money in (deposits, credits)" },
      { value: "money_out", label: "Money out (withdrawals, charges)" },
      { value: "card", label: "Card: charges are printed as positive amounts" },
    ]);
  });

  it("allows a second transactions table and refuses a fifth", () => {
    const two = [table("transactions"), table("transactions", { direction: "money_out" })];
    expect(problems(two)).toEqual([]);
    const four = [...two, table("transactions"), table("transactions")];
    expect(problems([...four, table("holdings")])).toEqual([]);
    expect(problems([...four, table("transactions")])).toEqual([
      "A layout takes at most four transaction tables.",
    ]);
    expect(problems([table("holdings"), table("holdings")])).toEqual([
      "A layout takes one holdings table.",
    ]);
  });

  it("previewSummary says whether the rows add up", () => {
    const base = { rows: [], row_count: 12, similar_waiting: 0, account_id: null };
    expect(previewSummary({ ...base, reconciliation: "ok", difference: "0.00" })).toBe(
      "12 rows read. They add up to the balances.",
    );
    expect(previewSummary({ ...base, reconciliation: "mismatch", difference: "-81.40" })).toBe(
      "12 rows read. They differ from the balances by -81.40.",
    );
    expect(previewSummary({ ...base, reconciliation: "mismatch", difference: null })).toBe(
      "12 rows read. They do not add up to the balances.",
    );
    expect(previewSummary({ ...base, reconciliation: "unavailable", difference: null })).toBe(
      "12 rows read. There are no balances to check them against.",
    );
    expect(previewSummary({ ...base, reconciliation: null, difference: null })).toBe(
      "12 rows read. There are no balances to check them against.",
    );
  });

  it("says that an earlier import of the file was replaced", () => {
    expect(warningText("rows_already_imported")).toBe(
      "The rows imported earlier from this file were replaced by this reading; rows you had changed were kept.",
    );
  });
});

/** Opens the dialog from the review page and labels the header line as a transaction table. */
async function openWithTable(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
  const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
  await user.selectOptions(await within(dialog).findByLabelText("Choose a line"), "1");
  await user.click(within(dialog).getByRole("button", { name: "Add this line" }));
  await user.selectOptions(within(dialog).getByLabelText("What is this?"), "transactions");
  await user.selectOptions(within(dialog).getByLabelText("Column 1: Booked"), "date");
  await user.selectOptions(within(dialog).getByLabelText("Column 2: Narrative"), "payee");
  await user.selectOptions(within(dialog).getByLabelText("Column 3: Movement"), "amount");
  return dialog;
}

const SHOWN_ROWS = [
  { posted_date: "2026-04-07", amount: "-62.57", payee: "Grocer", table: 0, page: 1 },
  { posted_date: "2026-04-09", amount: "-18.83", payee: null, table: 0, page: 1 },
];

describe("teach dialog", () => {
  it("labels a selection, describes another with free text and posts the labels", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut(),
        appliedOut({
          notes: ["The society's branch code"],
          warnings: ["no_label:closing_balance"],
        }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    const lines = await within(dialog).findByLabelText("Choose a line");
    const add = within(dialog).getByRole("button", { name: "Add this line" });
    expect(add).toBeDisabled();
    // Choosing a line (as arrowing through the list does) adds nothing yet.
    await user.selectOptions(lines, "0");
    expect(within(dialog).queryByLabelText("What is this?")).toBeNull();
    await user.selectOptions(lines, "1");
    await user.selectOptions(lines, "0");
    expect(within(dialog).queryAllByTestId("selection-box")).toHaveLength(0);
    await user.click(add);
    expect(within(dialog).getAllByTestId("selection-box")).toHaveLength(1);
    const what = within(dialog).getByLabelText("What is this?");
    const offered = within(what)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(offered).toContain(OTHER_LABEL);
    expect(offered).toContain(IGNORE_LABEL);
    await user.selectOptions(what, "closing_balance");

    await user.selectOptions(within(dialog).getByLabelText("Choose a line"), "1");
    await user.click(within(dialog).getByRole("button", { name: "Add this line" }));
    const second = within(dialog).getAllByLabelText("What is this?")[1];
    expect(second).toBeDefined();
    if (second === undefined) {
      return;
    }
    expect(within(dialog).queryByLabelText("Describe it")).toBeNull();
    await user.selectOptions(second, "other");
    await user.type(within(dialog).getByLabelText("Describe it"), "The society's branch code");
    expect(within(dialog).getAllByTestId("selection-box")).toHaveLength(2);

    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    expect(apply).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[0]?.body).toMatchObject({
      preview: true,
    });
    await user.click(apply);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[1]?.body).toEqual({
        labels: [
          { field: "closing_balance", page: 1, bbox: [60, 40, 190, 50] },
          {
            field: "other",
            page: 1,
            bbox: [60, 80, 450, 90],
            note: "The society's branch code",
          },
        ],
        tables: [],
        remember: false,
        reprocess_similar: true,
        replace: false,
        resolve: true,
        preview: false,
        item_id: "r-1",
      });
    });
    expect(
      await within(dialog).findByText(/No label is printed next to the closing balance/),
    ).toBeInTheDocument();
  });

  it("asks before replacing another taught layout and says what earlier teaching is kept", async () => {
    let calls = 0;
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": (call) => {
        if (isPreview(call.body)) {
          return json(previewOut());
        }
        calls += 1;
        return calls === 1
          ? problem(409, "layout-conflict")
          : json({
              document_id: "d-1",
              fields: [],
              tables: [],
              ignored: 0,
              notes: [],
              applied: [],
              warnings: ["item_left_open"],
              reprocessed: ["d-1"],
              fingerprint_id: "f-2",
              merged_from: 1,
              kept: ["period_end", "table:transactions"],
            });
      },
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    await user.type(within(dialog).getByLabelText("Institution"), "Finch Society");
    expect(within(dialog).queryByLabelText(/Replace the layout remembered/)).toBeNull();
    await user.click(within(dialog).getByLabelText("Remember this layout for the next files"));
    expect(within(dialog).getByLabelText(/Replace the layout remembered/)).not.toBeChecked();
    const apply = within(dialog).getByRole("button", { name: "Apply and remember" });
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    expect(
      await within(dialog).findByText(/Another layout of this institution was taught before/),
    ).toBeInTheDocument();
    // Replacing changes what is remembered, not what is read: the preview still stands.
    await user.click(within(dialog).getByLabelText(/Replace the layout remembered/));
    expect(apply).toBeEnabled();
    await user.click(apply);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[2]?.body).toMatchObject({
        remember: true,
        replace: true,
      });
    });
    expect(
      await within(dialog).findByText(/Still remembered from earlier teaching: statement end date, the transaction table/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/The review item stays open/)).toBeInTheDocument();
  });

  it("lets the same layout be taught afresh without a conflict first", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut(),
        appliedOut({ reprocessed: ["d-1"], fingerprint_id: "f-3", merged_from: null, kept: [] }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    await user.type(within(dialog).getByLabelText("Institution"), "Finch Society");
    await user.click(within(dialog).getByLabelText("Remember this layout for the next files"));
    expect(within(dialog).getByText(/drop earlier lines, notes or labels taught by mistake/)).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText(/Replace the layout remembered/));
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    const apply = within(dialog).getByRole("button", { name: "Apply and remember" });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[1]?.body).toMatchObject({
        remember: true,
        replace: true,
        preview: false,
      });
    });
  });

  it("apply is disabled until a preview was shown", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 2 }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    expect(apply).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    const posts = api.callsTo("POST", "/api/v1/documents/:id/teach");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toMatchObject({
      preview: true,
      tables: [{ kind: "transactions", direction: "as_printed", continues: false }],
    });
  });

  it("changing a direction clears the preview", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 2 }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    const preview = within(dialog).getByRole("button", { name: "Preview" });
    await user.click(preview);
    expect(await within(dialog).findByText(/2 rows read\./)).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Direction"), "money_out");
    expect(within(dialog).queryByText(/2 rows read\./)).toBeNull();
    expect(apply).toBeDisabled();
    await user.click(preview);
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    // The continuation and the account are part of the reading too.
    await user.click(
      within(dialog).getByLabelText(
        "The table continues on the following pages without its header",
      ),
    );
    expect(apply).toBeDisabled();
    await user.click(preview);
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.selectOptions(within(dialog).getByLabelText("Account for this file"), "a-2");
    expect(apply).toBeDisabled();
    await user.click(preview);
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")).toHaveLength(5);
    });
    expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[4]?.body).toMatchObject({
      preview: false,
      account_id: "a-2",
      tables: [{ kind: "transactions", direction: "money_out", continues: true }],
    });
  });

  it("the preview lists rows and says whether they add up", async () => {
    reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({
          rows: SHOWN_ROWS,
          row_count: 14,
          reconciliation: "mismatch",
          difference: "-81.40",
        }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(
      await within(dialog).findByText("14 rows read. They differ from the balances by -81.40."),
    ).toBeInTheDocument();
    const rows = within(dialog).getByRole("table", { name: "First rows read" });
    expect(within(rows).getAllByRole("row")).toHaveLength(3);
    const first = within(rows).getAllByRole("row")[1];
    expect(first).toBeDefined();
    if (first === undefined) {
      return;
    }
    expect(within(first).getByText("2026-04-07")).toBeInTheDocument();
    expect(within(first).getByText("Grocer")).toBeInTheDocument();
    expect(within(first).getByText("-62.57")).toBeInTheDocument();
    expect(within(first).getByText("Selection 1")).toBeInTheDocument();
    // A viewer who may not see the file's content gets no payee text.
    expect(within(rows).getByText("Not shown")).toBeInTheDocument();
    expect(within(dialog).getByText("The first 2 of 14 rows are shown.")).toBeInTheDocument();
    // Only the summary is announced; the rows are not read out cell by cell.
    expect(
      within(dialog).getByText("14 rows read. They differ from the balances by -81.40."),
    ).toHaveAttribute("role", "status");
    expect(rows.closest('[role="status"]')).toBeNull();
    expect(within(dialog).getByRole("region", { name: "Preview" })).toContainElement(rows);
  });

  it("any change to a selection clears the preview", async () => {
    reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 2 }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    const preview = within(dialog).getByRole("button", { name: "Preview" });
    const again = async () => {
      await user.click(preview);
      await waitFor(() => {
        expect(apply).toBeEnabled();
      });
    };
    await again();
    // The role of a column.
    await user.selectOptions(within(dialog).getByLabelText("Column 2: Narrative"), "memo");
    expect(within(dialog).queryByText(/2 rows read\./)).toBeNull();
    expect(apply).toBeDisabled();
    await again();
    // The institution the count of waiting files was taken for.
    await user.type(within(dialog).getByLabelText("Institution"), "H");
    expect(apply).toBeDisabled();
    await again();
    // A selection added, then what it is, then the selection removed.
    await user.selectOptions(within(dialog).getByLabelText("Choose a line"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Add this line" }));
    expect(apply).toBeDisabled();
    const second = within(dialog).getAllByLabelText("What is this?")[1];
    expect(second).toBeDefined();
    if (second === undefined) {
      return;
    }
    await user.selectOptions(second, "closing_balance");
    await again();
    await user.selectOptions(second, "opening_balance");
    expect(apply).toBeDisabled();
    await again();
    await user.click(within(dialog).getByRole("button", { name: "Remove selection 2" }));
    expect(within(dialog).queryByText(/2 rows read\./)).toBeNull();
    expect(apply).toBeDisabled();
  });

  it("names each table of the outcome when several were taught", async () => {
    const columns = [{ header: "Booked", role: "date" }];
    reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 12 }),
        appliedOut({
          tables: [
            { kind: "transactions", rows: 7, columns },
            { kind: "transactions", rows: 5, columns },
            { kind: "holdings", rows: 3, columns },
          ],
        }),
      ),
    });
    const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    expect(
      await within(dialog).findByText("Transaction table 1: 7 row(s) read."),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Transaction table 2: 5 row(s) read.")).toBeInTheDocument();
    // The only table of its kind needs no number.
    expect(within(dialog).getByText("Holdings table: 3 row(s) read.")).toBeInTheDocument();
    const duplicateKeys = logged.mock.calls.filter((call) =>
      call.some((part) => typeof part === "string" && part.includes("same key")),
    );
    logged.mockRestore();
    expect(duplicateKeys).toEqual([]);
  });

  it("after an apply the preview is gone and the same apply cannot be sent twice", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 2 }),
        appliedOut({ tables: [{ kind: "transactions", rows: 2, columns: [] }] }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    await user.type(within(dialog).getByLabelText("Institution"), "Harbourmoss Mutual");
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    expect(within(dialog).getByText("Nothing is saved until you apply.")).toBeInTheDocument();
    await user.click(apply);
    expect(await within(dialog).findByText(/Applied to this file\./)).toBeInTheDocument();
    expect(within(dialog).getByText("Transaction table: 2 row(s) read.")).toBeInTheDocument();
    expect(within(dialog).queryByText("Nothing is saved until you apply.")).toBeNull();
    expect(within(dialog).queryByRole("table", { name: "First rows read" })).toBeNull();
    expect(apply).toBeDisabled();
    await user.click(apply);
    expect(api.callsTo("POST", "/api/v1/documents/:id/teach")).toHaveLength(2);
    // Asking for something else (remembering the layout) is a new apply.
    await user.click(within(dialog).getByLabelText("Remember this layout for the next files"));
    const remember = within(dialog).getByRole("button", { name: "Apply and remember" });
    expect(remember).toBeEnabled();
    await user.click(remember);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")).toHaveLength(3);
    });
    expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[2]?.body).toMatchObject({
      preview: false,
      remember: true,
    });
  });

  it("says how many waiting files will be read again", async () => {
    reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut({ rows: SHOWN_ROWS, row_count: 2, similar_waiting: 3 }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    const dialog = await openWithTable(user);
    await user.type(within(dialog).getByLabelText("Institution"), "Harbourmoss Mutual");
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(await within(dialog).findByText(/2 rows read\./)).toBeInTheDocument();
    const waiting = "3 other waiting file(s) with this layout will be read again.";
    // Only a remembered layout reads the other files again.
    expect(within(dialog).queryByText(waiting)).toBeNull();
    await user.click(within(dialog).getByLabelText("Remember this layout for the next files"));
    expect(within(dialog).getByText(waiting)).toBeInTheDocument();
    await user.click(
      within(dialog).getByLabelText("Read files already received with this layout again"),
    );
    expect(within(dialog).queryByText(waiting)).toBeNull();
  });

  it("the account select starts on the document's account", async () => {
    const api = reviewApi(
      item(),
      { "POST /api/v1/documents/:id/teach": teachHandler(previewOut()) },
      "a-2",
    );
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    const select = within(dialog).getByLabelText("Account for this file");
    await waitFor(() => {
      expect(select).toHaveValue("a-2");
    });
    expect(within(select).getByRole("option", { name: "Not chosen" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Everyday" })).toBeInTheDocument();
    await user.selectOptions(await within(dialog).findByLabelText("Choose a line"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Add this line" }));
    await user.selectOptions(within(dialog).getByLabelText("What is this?"), "closing_balance");
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[0]?.body).toMatchObject({
        account_id: "a-2",
        preview: true,
      });
    });
    await user.selectOptions(select, "");
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")).toHaveLength(2);
    });
    expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[1]?.body).not.toHaveProperty(
      "account_id",
    );
  });

  it("the outcome links to what PerFin has learned", async () => {
    reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": teachHandler(
        previewOut(),
        appliedOut({ warnings: ["rows_already_imported"] }),
      ),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    expect(within(dialog).queryByRole("link", { name: "See what PerFin has learned" })).toBeNull();
    await user.selectOptions(await within(dialog).findByLabelText("Choose a line"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Add this line" }));
    await user.selectOptions(within(dialog).getByLabelText("What is this?"), "closing_balance");
    await user.click(within(dialog).getByRole("button", { name: "Preview" }));
    const apply = within(dialog).getByRole("button", { name: "Apply to this file" });
    await waitFor(() => {
      expect(apply).toBeEnabled();
    });
    await user.click(apply);
    const link = await within(dialog).findByRole("link", { name: "See what PerFin has learned" });
    expect(link).toHaveAttribute("href", "/settings/learned");
    expect(within(dialog).getByText(/were replaced by this reading/)).toBeInTheDocument();
  });

  it("words what was kept and the new warnings", () => {
    expect(keptText(["account_number", "table:holdings"])).toBe("account number, the holdings table");
    expect(warningText("item_left_open")).toMatch(/stays open/);
    expect(warningText("note_without_words")).toMatch(/no numbers/);
    // What the reader itself was unsure of is said in words, not as a code.
    expect(warningText("table_order_guessed")).toBe(
      "Two tables have the same header and one of them is missing on a page, so which rows are money in and which are money out is a guess. The statement waits in Review.",
    );
    expect(warningText("date_order_guessed")).toBe(
      "The dates could be read day first or month first; PerFin chose one. Check the dates of the rows.",
    );
  });

  it("enters the cost basis of an opening position", async () => {
    const detail = item({
      kind: "low_confidence_field",
      reason: "investment_opening_basis_unknown",
      payload: {
        reason: "investment_opening_basis_unknown",
        account_id: "a-1",
        securities: [
          {
            security_id: "s-1",
            quantity: "12.5000000000",
            trade_date: "2026-03-02",
            transaction_id: "it-1",
          },
        ],
      },
      next_step: "correct_fields",
    });
    const api = reviewApi(detail, {
      "PATCH /api/v1/investments/transactions/:id": () => json({ id: "it-1" }),
      "GET /api/v1/securities": () =>
        json({ items: [{ id: "s-1", symbol: "SGRX", name: "Synthetic Growth Fund" }], next_cursor: null }),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    // The card names the security, and the quantity drops its padding zeros.
    expect(await screen.findByText(/SGRX · Synthetic Growth Fund: 12\.5 units held before/)).toBeInTheDocument();
    const price = await screen.findByLabelText("Cost per unit");
    const save = screen.getByRole("button", { name: "Save cost basis" });
    expect(save).toBeDisabled();
    await user.type(price, "41.25");
    await user.click(save);
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/investments/transactions/:id")[0]?.body).toEqual({
        price: "41.25",
      });
    });
    expect(api.callsTo("PATCH", "/api/v1/investments/transactions/:id")[0]?.path).toBe(
      "/api/v1/investments/transactions/it-1",
    );
  });

  it("shows the lines a table import skipped, as the file prints them", async () => {
    const detail = item({
      kind: "low_confidence_field",
      reason: "lines_skipped",
      payload: {
        reason: "lines_skipped",
        count: 2,
        unknown_activity: 1,
        unknown_actions: [{ action: "mystery thing", rows: 1 }],
        header_row: 1,
        unknown_rows: [3],
        unreadable_rows: [4],
      },
      next_step: null,
    });
    const api = reviewApi(detail, {
      "GET /api/v1/imports/:id/preview": () =>
        json({
          tables: [
            {
              index: 0,
              rows: [
                ["Run Date", "Action", "Unused", "Amount"],
                ["03/02/2026", "YOU BOUGHT SGRX", "", "-20"],
                ["03/03/2026", "MYSTERY THING HAPPENED", "", "-3"],
                ["03/04/2026", "DIVIDEND RECEIVED SGRX", "", "not a number"],
              ],
            },
          ],
          statements: [],
        }),
    });
    renderApp("/inbox");
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Action" })).toBeInTheDocument();
    // A column empty in the header and in every shown line is left out.
    expect(within(table).queryByRole("columnheader", { name: "Unused" })).not.toBeInTheDocument();
    const lines = within(table).getAllByRole("row").slice(1);
    expect(lines.map((line) => line.textContent)).toEqual([
      "Unknown action03/03/2026MYSTERY THING HAPPENED-3",
      "Could not read03/04/2026DIVIDEND RECEIVED SGRXnot a number",
    ]);
    const call = api.callsTo("GET", "/api/v1/imports/:id/preview")[0];
    expect(call?.search).toContain("offset=1");
    expect(call?.search).toContain("limit=4");
  });

  it("enters the balances of a statement that does not add up", async () => {
    // The balances are typed in the reconcile view the card opens: the card
    // itself has no balance form, and nothing is sent to the old correction route.
    const detail = item({
      kind: "low_confidence_field",
      reason: "reconciliation_failed",
      payload: {
        reason: "reconciliation_failed",
        account_id: "a-1",
        statement: 0,
        opening: "100.00",
        closing: "150.00",
        difference: "-10.00",
      },
      next_step: "correct_fields",
    });
    const read = {
      statement: 0,
      account_id: "a-1",
      account_name: "Everyday",
      can_edit: true,
      causes: [],
      opening: "100.00",
      closing: "150.00",
      previous_closing: null,
      currency: "USD",
      total: "40.00",
      difference: "-10.00",
      lines_skipped: 0,
      period_start: "2026-04-01",
      period_end: "2026-04-30",
      printed_sign: "ledger",
      removed: [],
      rows: [
        {
          transaction_id: "t-1",
          posted_date: "2026-04-07",
          payee: "Grocer refund",
          amount: "40.00",
          origin: "read",
          edited: false,
          in_period: true,
          review_state: "pending",
          page: 1,
          bbox: [60, 100, 450, 110],
        },
      ],
      rows_truncated: false,
      status: "off",
      verification: "unverified",
    };
    const api = reviewApi(detail, {
      "GET /api/v1/documents/:id/reconciliation": () =>
        json({ document_id: "d-1", statements: [read] }),
      "POST /api/v1/documents/:id/reconciliation/confirm": () =>
        json({
          confirmed: 1,
          items_closed: 1,
          snapshot: true,
          statement: {
            ...read,
            closing: "140.00",
            difference: "0.00",
            status: "balanced",
            verification: "reconciled",
          },
        }),
    });
    renderApp("/inbox");
    const user = userEvent.setup();
    expect(await screen.findByText("ledger.pdf")).toBeInTheDocument();
    expect(screen.queryByLabelText("Closing balance")).not.toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: "Check against the statement" }),
    );
    const dialog = await screen.findByRole("dialog");
    const closing = await within(dialog).findByLabelText("Closing balance");
    expect(closing).toHaveValue("150.00");
    const confirm = within(dialog).getByRole("button", {
      name: "Confirm and record the balance",
    });
    expect(confirm).toBeDisabled();
    await user.clear(closing);
    await user.type(closing, "140.00");
    await user.click(confirm);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/documents/:id/reconciliation/confirm")[0]?.body,
      ).toEqual({
        statement: 0,
        opening_balance: "100.00",
        closing_balance: "140.00",
      });
    });
    expect(api.callsTo("POST", "/api/v1/review/:id/correct")).toHaveLength(0);
  });
});
