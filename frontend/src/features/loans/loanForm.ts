// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The loan terms form as data, shared by "New loan" and "Edit loan":
 * validation and the request bodies. Amounts stay decimal strings and the
 * rate is typed as a percent and sent as a fraction. An edit sends only the
 * terms that differ from the loan (`PATCH /loans/{id}` leaves absent fields
 * alone).
 */
import { isIsoDate } from "../../lib/dates";
import { amountSign, compareAmounts, decimalToString, isDecimalString, tryParseDecimal } from "../../lib/money";
import { percentToFraction } from "../investments/format";
import type { Loan, LoanCreate, LoanUpdate } from "./api";

export type RateType = Loan["rate_type"];

export interface LoanForm {
  principal: string;
  /** Yearly rate in percent units, as typed ("6.125"). */
  rate: string;
  rateType: RateType;
  term: string;
  origination: string;
  /** Principal and interest per month; blank for the level payment. */
  payment: string;
  /** Day of the month a payment is due; blank on a new loan for the origination date's day. */
  paymentDay: string;
  escrow: string;
  lender: string;
}

export type LoanProblems = Partial<Record<"account" | "name" | "principal" | "rate" | "term" | "origination" | "payment" | "paymentDay" | "escrow", string>>;

/** The account types a loan may have (`LOAN_ACCOUNT_TYPES` on the server). */
export type LoanAccountType = "loan" | "mortgage" | "line_of_credit";

/** Where a new loan goes: an account that exists, or one opened with the loan. */
export type LoanTarget = { id: string; currency: string } | { name: string; type: LoanAccountType; currency: string };

export const ACCOUNT_NAME_MAX = 120;

export type LoanBody<B> = { ok: true; body: B } | { ok: false; problems: LoanProblems };

export const TERM_MONTHS_MAX = 1200;
export const LENDER_MAX = 200;
/** Digits the server keeps after the point of a rate fraction (six of a percent). */
const RATE_FRACTION_DIGITS = 8;

export function emptyLoanForm(today: string): LoanForm {
  return { principal: "", rate: "", rateType: "fixed", term: "", origination: today, payment: "", paymentDay: "", escrow: "", lender: "" };
}

/** A rate fraction ("0.06125") as the percent a person types ("6.125"); "" if malformed. */
export function fractionToPercentText(fraction: string): string {
  const value = tryParseDecimal(fraction);
  if (value === null) {
    return "";
  }
  let units = value.scale >= 2 ? value.units : value.units * 10n ** BigInt(2 - value.scale);
  let scale = Math.max(value.scale - 2, 0);
  while (scale > 0 && units % 10n === 0n) {
    units /= 10n;
    scale -= 1;
  }
  return decimalToString({ units, scale });
}

/** The form filled with the terms of an existing loan. */
export function loanFormOf(loan: Loan): LoanForm {
  return {
    principal: loan.principal_original.amount,
    rate: fractionToPercentText(loan.rate),
    rateType: loan.rate_type,
    term: String(loan.term_months),
    origination: loan.origination_date,
    payment: loan.payment_amount.amount,
    paymentDay: String(loan.payment_day),
    escrow: loan.escrow_amount?.amount ?? "",
    lender: loan.lender ?? "",
  };
}

interface Checked {
  problems: LoanProblems;
  /** The rate as a fraction; null when the rate is a problem. */
  fraction: string | null;
}

function check(form: LoanForm): Checked {
  const problems: LoanProblems = {};
  const principal = form.principal.trim();
  if (!isDecimalString(principal) || amountSign(principal) <= 0) {
    problems.principal = "Enter the original amount, above zero.";
  }
  let fraction = percentToFraction(form.rate);
  if (fraction === null || amountSign(fraction) < 0 || compareAmounts(fraction, "1") >= 0) {
    problems.rate = "Enter the yearly rate as a percent, e.g. 6.125.";
    fraction = null;
  } else if ((tryParseDecimal(fraction)?.scale ?? 0) > RATE_FRACTION_DIGITS) {
    problems.rate = "Enter the rate with at most six decimal places.";
    fraction = null;
  }
  const term = form.term.trim();
  if (!/^\d{1,4}$/.test(term) || Number(term) < 1 || Number(term) > TERM_MONTHS_MAX) {
    problems.term = `Enter the term in months, from 1 to ${String(TERM_MONTHS_MAX)}.`;
  }
  if (!isIsoDate(form.origination)) {
    problems.origination = "Enter a valid date.";
  }
  const payment = form.payment.trim();
  if (payment !== "" && (!isDecimalString(payment) || amountSign(payment) <= 0)) {
    problems.payment = "Enter a payment above zero, or leave it blank.";
  }
  const day = form.paymentDay.trim();
  if (day !== "" && (!/^\d{1,2}$/.test(day) || Number(day) < 1 || Number(day) > 31)) {
    problems.paymentDay = "Enter a day of the month, from 1 to 31.";
  }
  const escrow = form.escrow.trim();
  if (escrow !== "" && (!isDecimalString(escrow) || amountSign(escrow) < 0)) {
    problems.escrow = "Enter an escrow amount of zero or more.";
  }
  return { problems, fraction };
}

/**
 * `POST /loans` on `target` (an existing account, or the name and type of the
 * account to open with the loan); blank optional terms are left to the
 * server's defaults.
 */
export function buildLoanCreate(form: LoanForm, target: LoanTarget | undefined): LoanBody<LoanCreate> {
  const { problems, fraction } = check(form);
  if (target === undefined) {
    problems.account = "Choose a loan account.";
  } else if (!("id" in target) && target.name.trim() === "") {
    problems.name = "Give the loan a name.";
  }
  if (Object.keys(problems).length > 0 || target === undefined || fraction === null) {
    return { ok: false, problems };
  }
  const currency = target.currency;
  const payment = form.payment.trim();
  const day = form.paymentDay.trim();
  const escrow = form.escrow.trim();
  const lender = form.lender.trim();
  return {
    ok: true,
    body: {
      ...("id" in target ? { account_id: target.id } : { account: { name: target.name.trim(), type: target.type } }),
      principal_original: { amount: form.principal.trim(), currency },
      rate: fraction,
      rate_type: form.rateType,
      term_months: Number(form.term),
      origination_date: form.origination,
      ...(payment === "" ? {} : { payment_amount: { amount: payment, currency } }),
      ...(day === "" ? {} : { payment_day: Number(day) }),
      ...(escrow === "" ? {} : { escrow_amount: { amount: escrow, currency } }),
      ...(lender === "" ? {} : { lender }),
    },
  };
}

const sameAmount = (a: string, b: string): boolean => compareAmounts(a, b) === 0;

/**
 * `PATCH /loans/{id}` with the terms that differ from `loan` (an empty body
 * when nothing changed). A blank payment asks the server for the level
 * payment of the new terms (it takes a zero amount to mean that); a blank
 * escrow or lender clears it.
 */
export function buildLoanUpdate(form: LoanForm, loan: Loan): LoanBody<LoanUpdate> {
  const { problems, fraction } = check(form);
  if (form.paymentDay.trim() === "") {
    problems.paymentDay = "Enter a day of the month, from 1 to 31.";
  }
  if (Object.keys(problems).length > 0 || fraction === null) {
    return { ok: false, problems };
  }
  const currency = loan.principal_original.currency;
  const body: LoanUpdate = {};
  const principal = form.principal.trim();
  if (!sameAmount(principal, loan.principal_original.amount)) {
    body.principal_original = { amount: principal, currency };
  }
  if (!sameAmount(fraction, loan.rate)) {
    body.rate = fraction;
  }
  if (form.rateType !== loan.rate_type) {
    body.rate_type = form.rateType;
  }
  if (Number(form.term) !== loan.term_months) {
    body.term_months = Number(form.term);
  }
  if (form.origination !== loan.origination_date) {
    body.origination_date = form.origination;
  }
  const payment = form.payment.trim();
  if (payment === "") {
    body.payment_amount = { amount: "0", currency };
  } else if (!sameAmount(payment, loan.payment_amount.amount)) {
    body.payment_amount = { amount: payment, currency };
  }
  if (Number(form.paymentDay) !== loan.payment_day) {
    body.payment_day = Number(form.paymentDay);
  }
  const escrow = form.escrow.trim();
  if (escrow === "") {
    if (loan.escrow_amount !== null) {
      body.escrow_amount = null;
    }
  } else if (loan.escrow_amount === null || !sameAmount(escrow, loan.escrow_amount.amount)) {
    body.escrow_amount = { amount: escrow, currency };
  }
  const lender = form.lender.trim();
  if (lender !== (loan.lender ?? "")) {
    body.lender = lender === "" ? null : lender;
  }
  return { ok: true, body };
}

/** Whether the amount, rate or term in `form` differs from `loan` (the terms a level payment follows). */
export function amortisationChanged(form: LoanForm, loan: Loan): boolean {
  const principal = form.principal.trim();
  const fraction = percentToFraction(form.rate);
  return (
    (isDecimalString(principal) && !sameAmount(principal, loan.principal_original.amount)) ||
    (fraction !== null && !sameAmount(fraction, loan.rate)) ||
    (/^\d{1,4}$/.test(form.term.trim()) && Number(form.term) !== loan.term_months)
  );
}
