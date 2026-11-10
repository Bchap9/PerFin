// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
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

describe("editing a loan", () => {
  const schedule = { loan_id: LOAN, basis: "projected", start_balance: money("190000.00"), rows: [], totals: { payments: 327, principal: money("190000.00"), interest: money("205000.00"), extra: money("0"), escrow: money("0"), paid: money("395000.00"), payoff_date: "2054-01-15" } };
  const handlers = (extra: Record<string, Handler> = {}): Record<string, Handler> => ({
    ...ownerSession(),
    ...referenceHandlers(),
    "GET /api/v1/loans/:id": () => json(loan),
    "GET /api/v1/loans/:id/schedule": () => json(schedule),
    ...extra,
  });

  it("opens with the loan's terms and sends only the corrected ones", async () => {
    const state = { rate: "0.06125" };
    const api = mockApi(
      handlers({
        "GET /api/v1/loans/:id": () => json({ ...loan, rate: state.rate }),
        "PATCH /api/v1/loans/:id": () => {
          state.rate = "0.055";
          return json({ ...loan, rate: state.rate });
        },
      }),
    );
    renderApp(`/loans/${LOAN}`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit loan" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit loan" });
    expect(within(dialog).getByLabelText(/^Original amount \(USD\)/)).toHaveValue("200000.00");
    expect(within(dialog).getByLabelText(/^Yearly rate/)).toHaveValue("6.125");
    expect(within(dialog).getByLabelText(/^Term/)).toHaveValue("360");
    expect(within(dialog).getByLabelText(/^Origination date/)).toHaveValue("2024-01-15");
    expect(within(dialog).getByLabelText(/^Monthly payment/)).toHaveValue("1215.22");
    expect(within(dialog).getByLabelText(/^Payment day/)).toHaveValue("15");
    expect(within(dialog).getByLabelText(/^Lender/)).toHaveValue("Fictional Bank");
    expect(within(dialog).getByRole("link", { name: "edit the account" })).toHaveAttribute("href", `/accounts/${loan.account_id}`);

    const rate = within(dialog).getByLabelText(/^Yearly rate/);
    await user.clear(rate);
    await user.type(rate, "5.5");
    expect(dialog).toHaveTextContent("The payment stays at $1,215.22 unless you change it.");
    await user.clear(within(dialog).getByLabelText(/^Monthly payment/));
    await user.clear(within(dialog).getByLabelText(/^Lender/));
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => { expect(api.callsTo("PATCH", `/api/v1/loans/${LOAN}`)).toHaveLength(1); });
    const call = api.callsTo("PATCH", `/api/v1/loans/${LOAN}`)[0];
    expect(call?.body).toEqual({ rate: "0.055", payment_amount: { amount: "0", currency: "USD" }, lender: null });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Loan updated")).toBeInTheDocument();
    expect(await screen.findByText("5.500% fixed")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Edit loan" })).not.toBeInTheDocument();
  });

  it("checks the terms before sending and shows the server's refusal", async () => {
    const api = mockApi(handlers({ "PATCH /api/v1/loans/:id": () => problem(422, "invalid-payment-date") }));
    renderApp(`/loans/${LOAN}`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit loan" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit loan" });
    const term = within(dialog).getByLabelText(/^Term/);
    await user.clear(term);
    await user.type(term, "0");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByText("Enter the term in months, from 1 to 1200.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/loans/:id")).toHaveLength(0);

    await user.clear(term);
    await user.type(term, "360");
    const origination = within(dialog).getByLabelText(/^Origination date/);
    await user.clear(origination);
    await user.type(origination, "2025-06-01");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByText("detail of invalid-payment-date")).toBeInTheDocument();
    expect(api.callsTo("PATCH", `/api/v1/loans/${LOAN}`)[0]?.body).toEqual({ origination_date: "2025-06-01" });
    expect(screen.getByRole("dialog", { name: "Edit loan" })).toBeInTheDocument();
  });

  it("is not offered when the loan's account cannot be edited", async () => {
    const readOnly: Handler = async (call) => {
      const base = referenceHandlers()["GET /api/v1/accounts"] ?? (() => json({ items: [], next_cursor: null }));
      const body = (await (await base(call)).json()) as { items: Record<string, unknown>[] };
      return json({ items: body.items.map((entry) => ({ ...entry, can_edit: false })), next_cursor: null });
    };
    mockApi(handlers({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }), "GET /api/v1/accounts": readOnly }));
    renderApp(`/loans/${LOAN}`);
    expect(await screen.findByRole("heading", { name: "Checking" })).toBeInTheDocument();
    expect(await screen.findByText("6.125% fixed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit loan" })).not.toBeInTheDocument();
  });
});
