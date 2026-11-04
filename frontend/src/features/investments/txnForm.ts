// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The investment transaction entry form as data: validation and the request
 * body, with decimal-string arithmetic only. Signs follow the cash effect on
 * the account (backend `investments/transactions.py`): a buy costs money
 * (amount <= 0), a sale brings it in. People type positive numbers; the
 * sign is applied here.
 */
import {
  addAmounts,
  currencyFractionDigits,
  isDecimalString,
  isZeroAmount,
  negateAmount,
  amountSign,
  subtractAmounts,
} from "../../lib/money";
import { isIsoDate } from "../../lib/dates";
import type { InvestmentTxnCreate, InvestmentTxnType } from "./api";
import { multiplyAmounts } from "./format";

export const TXN_TYPE_LABELS: Record<InvestmentTxnType, string> = {
  buy: "Buy",
  sell: "Sell",
  dividend: "Dividend",
  interest: "Interest",
  fee: "Fee",
  split: "Split",
  transfer_in: "Transfer in",
  transfer_out: "Transfer out",
  reinvest: "Reinvested dividend",
  corporate_action: "Corporate action",
};

export interface TxnForm {
  type: InvestmentTxnType;
  accountId: string;
  /** Currency of the account (amounts and fees). */
  currency: string;
  securityId: string;
  tradeDate: string;
  quantity: string;
  price: string;
  /** Positive cash amount as the person reads it on the confirmation; blank to compute. */
  amount: string;
  fees: string;
  ratioNew: string;
  ratioOld: string;
  memo: string;
}

export function emptyTxnForm(today: string, currency = "USD"): TxnForm {
  return {
    type: "buy",
    accountId: "",
    currency,
    securityId: "",
    tradeDate: today,
    quantity: "",
    price: "",
    amount: "",
    fees: "",
    ratioNew: "",
    ratioOld: "",
    memo: "",
  };
}

const NEEDS_SECURITY = new Set<InvestmentTxnType>(["buy", "sell", "reinvest", "transfer_in", "transfer_out", "split"]);
const NEEDS_QUANTITY = new Set<InvestmentTxnType>(["buy", "sell", "reinvest", "transfer_in", "transfer_out"]);
const NEEDS_RATIO = new Set<InvestmentTxnType>(["split", "corporate_action"]);

/** Which inputs a type uses, to show only those. */
export function fieldsFor(type: InvestmentTxnType) {
  return {
    security: NEEDS_SECURITY.has(type) || type === "dividend" || type === "interest" || type === "fee" || type === "corporate_action",
    quantity: NEEDS_QUANTITY.has(type),
    price: type === "buy" || type === "sell" || type === "reinvest" || type === "transfer_in",
    amount: type !== "split",
    fees: type === "buy" || type === "sell" || type === "reinvest",
    ratio: NEEDS_RATIO.has(type),
  };
}

export type TxnProblems = Partial<Record<"account" | "security" | "date" | "quantity" | "price" | "amount" | "fees" | "ratio", string>>;

export type TxnResult = { ok: true; body: InvestmentTxnCreate } | { ok: false; problems: TxnProblems };

function positive(value: string): boolean {
  return isDecimalString(value) && amountSign(value) > 0;
}

/** Amount before the sign: typed, or quantity x price (+/- fees). */
export function totalAmount(form: TxnForm): string | null {
  const typed = form.amount.trim();
  if (typed !== "") {
    return isDecimalString(typed) ? typed : null;
  }
  if (form.type !== "buy" && form.type !== "sell" && form.type !== "reinvest") {
    return null;
  }
  const gross = multiplyAmounts(form.quantity.trim(), form.price.trim(), currencyFractionDigits(form.currency));
  if (gross === null) {
    return null;
  }
  const fees = form.fees.trim() === "" ? "0" : form.fees.trim();
  if (!isDecimalString(fees)) {
    return null;
  }
  return form.type === "sell" ? subtractAmounts(gross, fees) : addAmounts(gross, fees);
}

/** Apply the cash-effect sign for the type. */
function signed(type: InvestmentTxnType, amount: string): string {
  switch (type) {
    case "buy":
    case "transfer_in":
    case "transfer_out":
    case "fee":
      return amountSign(amount) > 0 ? negateAmount(amount) : amount;
    case "reinvest":
      return amountSign(amount) > 0 ? negateAmount(amount) : amount;
    default:
      return amount;
  }
}

export function buildTxnBody(form: TxnForm): TxnResult {
  const problems: TxnProblems = {};
  const used = fieldsFor(form.type);
  if (form.accountId === "") {
    problems.account = "Choose an investment account.";
  }
  if (NEEDS_SECURITY.has(form.type) && form.securityId === "") {
    problems.security = "Choose a security.";
  }
  if (form.type === "corporate_action" && form.securityId === "") {
    problems.security = "Choose a security.";
  }
  if (!isIsoDate(form.tradeDate)) {
    problems.date = "Enter a valid date.";
  }
  const quantity = form.quantity.trim();
  if (used.quantity && !positive(quantity)) {
    problems.quantity = "Enter a quantity above zero.";
  }
  const price = form.price.trim();
  if (used.price && price !== "" && !(isDecimalString(price) && amountSign(price) >= 0)) {
    problems.price = "Enter a price of zero or more.";
  }
  if ((form.type === "buy" || form.type === "sell") && price === "" && form.amount.trim() === "") {
    problems.price = "Enter a price, or the total amount.";
  }
  const fees = form.fees.trim();
  if (used.fees && fees !== "" && !(isDecimalString(fees) && amountSign(fees) >= 0)) {
    problems.fees = "Enter fees of zero or more.";
  }
  let amount = "0";
  if (used.amount) {
    const total = totalAmount(form);
    const computedType = form.type === "buy" || form.type === "sell" || form.type === "reinvest";
    const alreadyExplained =
      computedType && form.amount.trim() === "" && (problems.price !== undefined || problems.quantity !== undefined || problems.fees !== undefined);
    if (total === null) {
      if (!alreadyExplained) {
        problems.amount = form.amount.trim() === "" ? "Enter the amount." : "Enter a valid amount.";
      }
    } else {
      amount = signed(form.type, total);
      if ((form.type === "buy" || form.type === "sell") && isZeroAmount(total) && price !== "0") {
        problems.amount = "The amount cannot be zero.";
      }
    }
  }
  if (used.ratio) {
    if (!positive(form.ratioNew.trim()) || !positive(form.ratioOld.trim())) {
      problems.ratio = "Enter both sides of the ratio, above zero.";
    }
  }
  if (Object.keys(problems).length > 0) {
    return { ok: false, problems };
  }
  const body: InvestmentTxnCreate = {
    account_id: form.accountId,
    type: form.type,
    trade_date: form.tradeDate,
    amount: { amount, currency: form.currency },
  };
  if (form.securityId !== "") {
    body.security_id = form.securityId;
  }
  if (used.quantity) {
    body.quantity = quantity;
  }
  if (used.price && price !== "") {
    body.price = price;
  }
  if (used.fees && fees !== "" && !isZeroAmount(fees)) {
    body.fees = { amount: fees, currency: form.currency };
  }
  if (used.ratio) {
    body.ratio = { new: form.ratioNew.trim(), old: form.ratioOld.trim() };
  }
  if (form.memo.trim() !== "") {
    body.memo = form.memo.trim();
  }
  return { ok: true, body };
}
