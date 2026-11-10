// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { problemFromBody } from "../../lib/api/client";
import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, PAYEE_CAFE, money, referenceHandlers } from "../planning/testing";
import { attachProblem, transactionLabel } from "./AttachDialog";
import type { DocumentLink, LedgerTransaction } from "./api";

const STAMP = "2026-11-03T10:00:00Z";
const HIDDEN_ACCOUNT = "a0000000-0000-4000-8000-0000000000ff";

const transaction = (id: string, extra: Partial<LedgerTransaction> = {}): LedgerTransaction => ({
  id,
  account_id: ACCOUNT_CHECKING,
  amount: money("-4.50"),
  posted_date: "2026-10-20",
  raw_payee: "CAFE UNO #12",
  memo: null,
  notes: null,
  category_id: null,
  payee_id: PAYEE_CAFE,
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
  ...extra,
});

const COFFEE = transaction("t-1");
const RENT = transaction("t-2", { amount: money("-900.00"), posted_date: "2026-10-01", raw_payee: "LANDLORD LLC", payee_id: null });

const link = (id: string, type: DocumentLink["target_type"], target: string): DocumentLink => ({
  id,
  document_id: "d-1",
  target_type: type,
  target_id: target,
  created_at: STAMP,
});

const detail = (extra: Record<string, unknown> = {}) => ({
  id: "d-1",
  account_id: null,
  created_at: STAMP,
  updated_at: STAMP,
  error: null,
  institution_id: null,
  kind: "receipt",
  mime_detected: "application/pdf",
  original_filename: "cafe-receipt.pdf",
  parser_version: null,
  period_end: null,
  period_start: null,
  sha256: "0".repeat(64),
  size: 2048,
  source: "upload",
  status: "processed",
  tax_year: null,
  uploaded_by: null,
  can_edit: true,
  downloadable: true,
  links: [] as DocumentLink[],
  open_review_items: 0,
  quarantined: true,
  ...extra,
});

function documentApi(extra: Record<string, Handler> = {}, start: Record<string, unknown> = {}) {
  const state = { links: [] as DocumentLink[] };
  const api = mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "member" }),
    ...referenceHandlers(),
    "GET /api/v1/documents/:id": () => json(detail({ ...start, links: [...((start["links"] as DocumentLink[] | undefined) ?? []), ...state.links] })),
    "GET /api/v1/transactions": (call) => json({ items: call.search.includes("q=landlord") ? [RENT] : [COFFEE, RENT], next_cursor: null }),
    "POST /api/v1/documents/:id/link": (call) => {
      const body = call.body as { target_type: DocumentLink["target_type"]; target_id: string };
      const created = link(`l-${String(state.links.length + 1)}`, body.target_type, body.target_id);
      state.links = [...state.links, created];
      return json(created, 201);
    },
    ...extra,
  });
  return { api, state };
}

describe("attach helpers", () => {
  it("names a transaction by date, payee and amount", () => {
    const payees = [{ id: PAYEE_CAFE, name: "Cafe Uno", normalized_name: "cafe uno", aliases: [], default_category_id: null, created_at: STAMP, updated_at: STAMP }];
    expect(transactionLabel(COFFEE, payees)).toBe("Oct 20, 2026, Cafe Uno, -$4.50");
    expect(transactionLabel(RENT, payees)).toBe("Oct 1, 2026, LANDLORD LLC, -$900.00");
    expect(transactionLabel(transaction("t-3", { raw_payee: null, payee_id: null }), undefined)).toBe("Oct 20, 2026, No payee, -$4.50");
  });

  it("turns a hidden target into words a person can act on", () => {
    const notFound = (slug: string) => problemFromBody(404, { type: `urn:perfin:problem:${slug}`, title: "Not found" });
    expect(attachProblem(notFound("transaction-not-found"))).toMatch(/in an account you may not change/);
    expect(attachProblem(notFound("account-not-found"))).toMatch(/you may not change it/);
    expect(attachProblem(problemFromBody(403, { type: "urn:perfin:problem:forbidden", title: "Forbidden", detail: "You may not change this document." }))).toBe(
      "You may not change this document.",
    );
  });
});

describe("what a document is attached to", () => {
  it("lists the account and transactions the detail response carries", async () => {
    documentApi(
      {
        "GET /api/v1/transactions/:id": (call) => (call.path.endsWith("/t-1") ? json(COFFEE) : problem(404, "transaction-not-found")),
      },
      {
        links: [link("l-a", "account", ACCOUNT_CHECKING), link("l-b", "transaction", "t-1"), link("l-c", "transaction", "t-gone"), link("l-d", "account", HIDDEN_ACCOUNT)],
      },
    );
    renderApp("/documents/d-1");
    const list = await screen.findByRole("list", { name: "Attached to" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(await within(list).findByRole("link", { name: "Checking" })).toHaveAttribute("href", `/accounts/${ACCOUNT_CHECKING}`);
    expect(items[0]).toHaveTextContent("Account Checking");
    await waitFor(() => {
      expect(items[1]).toHaveTextContent("Oct 20, 2026, Cafe Uno, Checking -$4.50");
    });
    await waitFor(() => {
      expect(items[2]).toHaveTextContent("A transaction you cannot see, or that was deleted");
    });
    expect(items[3]).toHaveTextContent("An account you cannot see");
  });

  it("offers no attaching to someone who may not change the document", async () => {
    const { api } = documentApi({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }) }, { can_edit: false });
    renderApp("/documents/d-1");
    expect(await screen.findByText(/Not attached to anything yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Attach to…" })).not.toBeInTheDocument();
    expect(api.calls.some((call) => call.method !== "GET")).toBe(false);
  });
});

describe("attach to", () => {
  it("finds a transaction by text and dates and attaches the document to it", async () => {
    const { api } = documentApi({ "GET /api/v1/transactions/:id": () => json(RENT) }, { period_start: "2026-10-01", period_end: "2026-10-31" });
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Attach to…" }));
    const dialog = await screen.findByRole("dialog", { name: "Attach to…" });
    expect(dialog).toHaveTextContent(/Nothing is imported or changed/);
    expect(dialog).toHaveTextContent(/an attachment cannot be removed again yet/);
    // The search starts from the document's own period.
    expect(within(dialog).getByLabelText("From date")).toHaveValue("2026-10-01");
    expect(within(dialog).getByLabelText("To date")).toHaveValue("2026-10-31");
    const results = await within(dialog).findByRole("list", { name: "Matching transactions" });
    expect(within(results).getAllByRole("listitem")).toHaveLength(2);
    expect(within(results).getAllByRole("listitem")[0]).toHaveTextContent("Oct 20, 2026, Cafe Uno, Checking -$4.50");

    await user.type(within(dialog).getByLabelText("Payee, memo or note contains"), "landlord");
    await user.clear(within(dialog).getByLabelText("To date"));
    await user.type(within(dialog).getByLabelText("To date"), "2026-10-15");
    await user.click(within(dialog).getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(within(dialog).getAllByRole("listitem")).toHaveLength(1);
    });
    const search = api.callsTo("GET", "/api/v1/transactions").at(-1)?.search ?? "";
    expect(search).toContain("q=landlord");
    expect(search).toContain("date_from=2026-10-01");
    expect(search).toContain("date_to=2026-10-15");
    expect(api.callsTo("POST", "/api/v1/documents/:id/link")).toHaveLength(0);

    await user.click(within(dialog).getByRole("button", { name: "Attach to Oct 1, 2026, LANDLORD LLC, -$900.00" }));
    expect(await screen.findByText("Document attached")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/documents/:id/link");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.path).toBe("/api/v1/documents/d-1/link");
    expect(calls[0]?.body).toEqual({ target_type: "transaction", target_id: "t-2" });
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(calls[0]?.headers.get("Idempotency-Key")).toMatch(/^[\x21-\x7e]{16,255}$/);
    // The dialog closes and the page shows the new link.
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Attach to…" })).not.toBeInTheDocument();
    });
    const list = await screen.findByRole("list", { name: "Attached to" });
    await waitFor(() => {
      expect(list).toHaveTextContent("Oct 1, 2026, LANDLORD LLC, Checking -$900.00");
    });
  });

  it("checks the dates before searching and says when nothing matches", async () => {
    const { api } = documentApi({ "GET /api/v1/transactions": () => json({ items: [], next_cursor: null }) });
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Attach to…" }));
    const dialog = await screen.findByRole("dialog", { name: "Attach to…" });
    expect(await within(dialog).findByText(/No transactions match/)).toBeInTheDocument();
    const before = api.callsTo("GET", "/api/v1/transactions").length;
    await user.type(within(dialog).getByLabelText("From date"), "2026-10-10");
    await user.type(within(dialog).getByLabelText("To date"), "2026-10-01");
    await user.click(within(dialog).getByRole("button", { name: "Search" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The first date is after the last date.");
    expect(api.callsTo("GET", "/api/v1/transactions")).toHaveLength(before);
  });

  it("shows the server's refusal and sends a retry with the same key", async () => {
    const { api } = documentApi({ "POST /api/v1/documents/:id/link": () => problem(404, "transaction-not-found") });
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Attach to…" }));
    const dialog = await screen.findByRole("dialog", { name: "Attach to…" });
    const attach = await within(dialog).findByRole("button", { name: "Attach to Oct 20, 2026, Cafe Uno, -$4.50" });
    await user.click(attach);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("That transaction no longer exists, or it is in an account you may not change.");
    await user.click(attach);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/link")).toHaveLength(2);
    });
    const keys = api.callsTo("POST", "/api/v1/documents/:id/link").map((call) => call.headers.get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]);
    // Another transaction is another request.
    await user.click(within(dialog).getByRole("button", { name: "Attach to Oct 1, 2026, LANDLORD LLC, -$900.00" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/link")).toHaveLength(3);
    });
    expect(api.callsTo("POST", "/api/v1/documents/:id/link").at(2)?.headers.get("Idempotency-Key")).not.toBe(keys[0]);
    expect(screen.queryByText("Document attached")).not.toBeInTheDocument();
  });

  it("attaches to an account and marks what is already attached", async () => {
    const { api } = documentApi(
      { "GET /api/v1/transactions/:id": () => json(COFFEE) },
      { links: [link("l-b", "transaction", "t-1")] },
    );
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Attach to…" }));
    const dialog = await screen.findByRole("dialog", { name: "Attach to…" });
    // The transaction it is already attached to is marked, not offered again.
    const results = await within(dialog).findByRole("list", { name: "Matching transactions" });
    expect(within(results).getAllByRole("listitem")[0]).toHaveTextContent("Attached");
    expect(within(dialog).queryByRole("button", { name: "Attach to Oct 20, 2026, Cafe Uno, -$4.50" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByLabelText("An account"));
    const attach = within(dialog).getByRole("button", { name: "Attach to this account" });
    expect(attach).toBeDisabled();
    await user.selectOptions(within(dialog).getByLabelText("Account"), ACCOUNT_CHECKING);
    await user.click(attach);
    expect(await screen.findByText("Document attached")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/documents/:id/link");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toEqual({ target_type: "account", target_id: ACCOUNT_CHECKING });
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");

    // Reopened, the account is shown as already attached.
    await user.click(await screen.findByRole("button", { name: "Attach to…" }));
    const again = await screen.findByRole("dialog", { name: "Attach to…" });
    await user.click(within(again).getByLabelText("An account"));
    expect(await within(again).findByRole("option", { name: "Checking (already attached)" })).toBeDisabled();
  });
});
