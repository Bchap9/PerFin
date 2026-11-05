// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { account, lookupHandlers, NOW } from "../transactions/fixtures";
import type { DetectedAccount } from "./api";
import { buildChoices, initialChoice } from "./DocumentAccountsDialog";

function detected(extra: Partial<DetectedAccount>): DetectedAccount {
  return {
    target: "investment",
    index: 0,
    status: "new",
    account_id: null,
    candidates: [],
    review_item_id: "r-1",
    last4: null,
    name: "Individual",
    institution_name: "Fidelity Investments",
    account_type: "brokerage",
    currency: "USD",
    period_start: "2026-10-01",
    period_end: "2026-10-31",
    opening_balance: null,
    closing_balance: null,
    transactions: 9,
    proposal: {
      name: "Fidelity Individual",
      type: "brokerage",
      currency: "USD",
      institution_name: "Fidelity Investments",
      number_last4: null,
      opening_balance: null,
      opening_balance_date: null,
      suggested_by_model: false,
    },
    ...extra,
  };
}

describe("document account choices", () => {
  it("creates new accounts from their proposals and skips known ones", () => {
    const found = [
      detected({}),
      detected({
        index: 1,
        status: "matched",
        account_id: "a-1",
        proposal: null,
        name: null,
        last4: "1111",
      }),
    ];
    const rows = found.map(initialChoice);
    expect(rows.map((row) => row.mode)).toEqual(["create", "skip"]);
    expect(buildChoices(found, rows)).toEqual({
      problems: [],
      items: [
        {
          target: "investment",
          index: 0,
          create: {
            type: "brokerage",
            name: "Fidelity Individual",
            include_in_net_worth: true,
            currency: "USD",
          },
          institution_name: "Fidelity Investments",
        },
      ],
    });
  });

  it("carries the last four digits and the statement's opening balance", () => {
    const found = [
      detected({
        target: "statement",
        last4: "4321",
        proposal: {
          name: "Synthetic Checking 4321",
          type: "checking",
          currency: "USD",
          institution_name: null,
          number_last4: "4321",
          opening_balance: "120.50",
          opening_balance_date: "2026-10-01",
          suggested_by_model: false,
        },
      }),
    ];
    const { items } = buildChoices(found, found.map(initialChoice));
    expect(items[0]?.create).toEqual({
      type: "checking",
      name: "Synthetic Checking 4321",
      include_in_net_worth: true,
      currency: "USD",
      number_last4: "4321",
      opening_balance: { amount: "120.50", currency: "USD" },
      opening_balance_date: "2026-10-01",
    });
  });

  it("asks for what is missing", () => {
    const found = [
      detected({}),
      detected({ index: 1, status: "ambiguous", candidates: [] }),
    ];
    const rows = found
      .map(initialChoice)
      .map((row, index) => (index === 0 ? { ...row, name: " " } : row));
    expect(buildChoices(found, rows).problems).toEqual([
      "Name the new account for Individual.",
      "Choose the account for Individual.",
    ]);
  });

  it("creates the accounts from the review queue and imports the file", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers([account("a-1", "Everyday")]),
      "GET /api/v1/review": () =>
        json({
          items: [
            {
              id: "r-1",
              kind: "unknown_account",
              state: "open",
              document_id: "d-1",
              document: null,
              reason: "account_not_found",
              payload: {
                reason: "account_not_found",
                statement: 0,
                investment: true,
              },
              resolution: null,
              resolved_at: null,
              resolved_by: null,
              created_at: NOW,
              next_step: "choose_account",
              can_resolve: true,
            },
          ],
          next_cursor: null,
        }),
      "GET /api/v1/documents/:id/accounts": () =>
        json({ document_id: "d-1", accounts: [detected({})] }),
      "POST /api/v1/documents/:id/accounts/apply": () =>
        json({ document_id: "d-1", created: ["a-9"], job_id: "j-1" }, 202),
    });
    renderApp("/review");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Set up accounts from this file",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText(/Individual · Fidelity Investments/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByDisplayValue("Fidelity Individual"),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", {
        name: "Create accounts and import",
      }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/documents/:id/accounts/apply"),
      ).toHaveLength(1);
    });
    expect(
      api.callsTo("POST", "/api/v1/documents/:id/accounts/apply")[0]?.body,
    ).toEqual({
      items: [
        {
          target: "investment",
          index: 0,
          create: {
            type: "brokerage",
            name: "Fidelity Individual",
            include_in_net_worth: true,
            currency: "USD",
          },
          institution_name: "Fidelity Investments",
        },
      ],
    });
  });
});
