// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Loan } from "./api";
import { amortisationChanged, buildLoanCreate, buildLoanUpdate, emptyLoanForm, fractionToPercentText, loanFormOf } from "./loanForm";

const STAMP = "2026-11-01T10:00:00Z";
const usd = (amount: string) => ({ amount, currency: "USD" });

function loan(patch: Partial<Loan> = {}): Loan {
  return {
    id: "l-1",
    account_id: "a-1",
    principal_original: usd("200000.00"),
    rate: "0.06125",
    rate_type: "fixed",
    term_months: 360,
    origination_date: "2024-01-15",
    payment_amount: usd("1215.22"),
    payment_day: 15,
    escrow_amount: usd("250.00"),
    lender: "Fictional Bank",
    maturity_date: "2054-01-15",
    created_at: STAMP,
    updated_at: STAMP,
    projection: {
      as_of: "2026-11-03",
      balance: usd("190000.00"),
      balance_source: "scheduled",
      rate: "0.06125",
      scheduled_payment: usd("1215.22"),
      next_due_date: "2026-11-15",
      payoff_date: "2054-01-15",
      remaining_payments: 327,
      remaining_interest: usd("205000.00"),
    },
    ...patch,
  };
}

describe("loan form", () => {
  it("shows a rate fraction as the percent a person types", () => {
    expect(fractionToPercentText("0.06125")).toBe("6.125");
    expect(fractionToPercentText("0.07000000")).toBe("7");
    expect(fractionToPercentText("0.5")).toBe("50");
    expect(fractionToPercentText("0")).toBe("0");
    expect(fractionToPercentText("six")).toBe("");
  });

  it("builds a new loan and leaves blank optional terms to the server", () => {
    const form = { ...emptyLoanForm("2026-11-03"), principal: " 15000 ", rate: "4.5", term: "60" };
    expect(buildLoanCreate(form, { id: "a-9", currency: "EUR" })).toEqual({
      ok: true,
      body: { account_id: "a-9", principal_original: { amount: "15000", currency: "EUR" }, rate: "0.045", rate_type: "fixed", term_months: 60, origination_date: "2026-11-03" },
    });
  });

  it("names every problem of a new loan", () => {
    const form = { ...emptyLoanForm("2026-11-03"), principal: "0", rate: "100", term: "1201", origination: "", payment: "-1", escrow: "x" };
    expect(buildLoanCreate(form, undefined)).toEqual({
      ok: false,
      problems: {
        account: "Choose a loan account.",
        principal: "Enter the original amount, above zero.",
        rate: "Enter the yearly rate as a percent, e.g. 6.125.",
        term: "Enter the term in months, from 1 to 1200.",
        origination: "Enter a valid date.",
        payment: "Enter a payment above zero, or leave it blank.",
        escrow: "Enter an escrow amount of zero or more.",
      },
    });
    expect(buildLoanCreate({ ...emptyLoanForm("2026-11-03"), principal: "1", rate: "6.1234567", term: "12" }, { id: "a", currency: "USD" })).toEqual({
      ok: false,
      problems: { rate: "Enter the rate with at most six decimal places." },
    });
  });

  it("sends nothing for an untouched edit, even when amounts are written differently", () => {
    const original = loan();
    expect(buildLoanUpdate(loanFormOf(original), original)).toEqual({ ok: true, body: {} });
    expect(buildLoanUpdate({ ...loanFormOf(original), principal: "200000", rate: "6.1250", escrow: "250" }, original)).toEqual({ ok: true, body: {} });
  });

  it("sends each corrected term, a zero payment for the level payment and null to clear", () => {
    const original = loan();
    const form = {
      ...loanFormOf(original),
      principal: "210000",
      rate: "5.5",
      rateType: "variable" as const,
      term: "300",
      origination: "2024-02-01",
      payment: "",
      paymentDay: "1",
      escrow: "",
      lender: " ",
    };
    expect(buildLoanUpdate(form, original)).toEqual({
      ok: true,
      body: {
        principal_original: usd("210000"),
        rate: "0.055",
        rate_type: "variable",
        term_months: 300,
        origination_date: "2024-02-01",
        payment_amount: usd("0"),
        payment_day: 1,
        escrow_amount: null,
        lender: null,
      },
    });
    const plain = loan({ escrow_amount: null, lender: null });
    expect(buildLoanUpdate({ ...loanFormOf(plain), payment: "1300.00", escrow: "100", lender: "New Bank" }, plain)).toEqual({
      ok: true,
      body: { payment_amount: usd("1300.00"), escrow_amount: usd("100"), lender: "New Bank" },
    });
  });

  it("needs a payment day on an existing loan", () => {
    const original = loan();
    expect(buildLoanUpdate({ ...loanFormOf(original), paymentDay: "" }, original)).toEqual({
      ok: false,
      problems: { paymentDay: "Enter a day of the month, from 1 to 31." },
    });
    expect(buildLoanUpdate({ ...loanFormOf(original), paymentDay: "32" }, original)).toEqual({
      ok: false,
      problems: { paymentDay: "Enter a day of the month, from 1 to 31." },
    });
  });

  it("knows when the terms a level payment follows have changed", () => {
    const original = loan();
    expect(amortisationChanged(loanFormOf(original), original)).toBe(false);
    expect(amortisationChanged({ ...loanFormOf(original), lender: "Other", origination: "2024-02-01" }, original)).toBe(false);
    expect(amortisationChanged({ ...loanFormOf(original), rate: "5.5" }, original)).toBe(true);
    expect(amortisationChanged({ ...loanFormOf(original), term: "300" }, original)).toBe(true);
    expect(amortisationChanged({ ...loanFormOf(original), principal: "1" }, original)).toBe(true);
  });
});
