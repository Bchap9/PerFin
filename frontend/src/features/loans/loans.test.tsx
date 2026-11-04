// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { money, ownerSession, referenceHandlers } from "../planning/testing";

const LOAN = "10000000-0000-4000-8000-000000000001";
const STAMP = "2026-11-01T10:00:00Z";

const loan = {
  id: LOAN,
  account_id: "a0000000-0000-4000-8000-000000000001",
  principal_original: money("200000.00"),
  rate: "0.06125",
  rate_type: "fixed",
  term_months: 360,
  origination_date: "2024-01-15",
  payment_amount: money("1215.22"),
  payment_day: 15,
  escrow_amount: null,
  lender: "Fictional Bank",
  maturity_date: "2054-01-15",
  created_at: STAMP,
  updated_at: STAMP,
  projection: {
    as_of: "2026-11-03",
    balance: money("190000.00"),
    balance_source: "scheduled",
    rate: "0.06125",
    scheduled_payment: money("1215.22"),
    next_due_date: "2026-11-15",
    payoff_date: "2054-01-15",
    remaining_payments: 327,
    remaining_interest: money("205000.00"),
  },
};

const scenario = (payoff: string, payments: number, interest: string, extra: string) => ({
  payoff_date: payoff,
  payments,
  total_interest: money(interest),
  total_extra: money(extra),
});

describe("loans", () => {
  it("lists loans with balance, rate and payoff", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers(), "GET /api/v1/loans": () => json({ items: [loan], next_cursor: null }) });
    renderApp("/loans");
    const row = within(await screen.findByRole("table", { name: "Loans" })).getByRole("row", { name: /Checking/ });
    expect(within(row).getByText("6.125%")).toBeInTheDocument();
    expect(within(row).getByText("$190,000.00")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Checking" })).toHaveAttribute("href", `/loans/${LOAN}`);
  });

  it("simulates an extra monthly payment and shows interest and time saved", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/loans/:id": () => json(loan),
      "GET /api/v1/loans/:id/schedule": () => json({ loan_id: LOAN, basis: "projected", start_balance: money("190000.00"), rows: [], totals: { payments: 327, principal: money("190000.00"), interest: money("205000.00"), extra: money("0"), escrow: money("0"), paid: money("395000.00"), payoff_date: "2054-01-15" } }),
      "POST /api/v1/loans/:id/simulate": () =>
        json({
          loan_id: LOAN,
          as_of: "2026-11-03",
          balance: money("190000.00"),
          baseline: scenario("2054-01-15", 327, "205000.00", "0.00"),
          with_extras: scenario("2049-03-15", 269, "150000.00", "20000.00"),
          interest_saved: money("55000.00"),
          payments_saved: 58,
          schedule: null,
        }),
    });
    renderApp(`/loans/${LOAN}`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Extra payments" }));
    const form = await screen.findByRole("form", { name: "Extra payment simulator" });

    await user.click(within(form).getByRole("button", { name: "Simulate" }));
    expect(await within(form).findByText(/Add a one-off payment or a recurring/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/loans/:id/simulate")).toHaveLength(0);

    await user.type(within(form).getByLabelText(/Extra per payment/), "100.00");
    await user.click(within(form).getByRole("button", { name: "Add a one-off payment" }));
    await user.type(within(form).getByLabelText(/Amount 1/), "5000");
    await user.click(within(form).getByRole("button", { name: "Simulate" }));

    expect(await screen.findByText("$55,000.00")).toBeInTheDocument();
    expect(screen.getByText("58")).toBeInTheDocument();
    const sent = api.callsTo("POST", "/api/v1/loans/:id/simulate")[0]?.body as { recurring: unknown; one_off: { amount: unknown }[] };
    expect(sent.recurring).toMatchObject({ amount: { amount: "100.00", currency: "USD" } });
    expect(sent.one_off[0]?.amount).toEqual({ amount: "5000", currency: "USD" });
  });
});
