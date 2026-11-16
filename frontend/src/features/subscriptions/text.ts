// SPDX-License-Identifier: AGPL-3.0-or-later
/** The words of the subscriptions view: cadence, status, kind and each change. */
import { formatDate } from "../../lib/dates";
import { amountSign, formatMoney, isDecimalString, negateAmount } from "../../lib/money";
import type { SubscriptionEvent, SubscriptionKind, SubscriptionStatus } from "./api";

export { cadenceText } from "../calendar/scheduleForm";

const STATUS_TEXT = new Map<SubscriptionStatus, string>([
  ["suggested", "Might be a subscription"],
  ["active", "Found for you"],
  ["confirmed", "Confirmed"],
  ["paused", "Paused"],
  ["ended", "Ended"],
  ["dismissed", "Not a subscription"],
]);

const KIND_TEXT = new Map<SubscriptionKind, string>([
  ["subscription", "Subscription"],
  ["bill", "Bill"],
  ["income", "Income"],
  ["other", "Other"],
]);

const BILL_STATUS_TEXT = new Map<SubscriptionStatus, string>([
  ["suggested", "Might be a bill"],
  ["dismissed", "Not a bill"],
]);

/** The status in words; a bill is not called a subscription where the words name the kind. */
export function statusText(status: SubscriptionStatus, kind?: SubscriptionKind): string {
  return (kind === "bill" ? BILL_STATUS_TEXT.get(status) : undefined) ?? STATUS_TEXT.get(status) ?? status;
}

export function kindText(kind: SubscriptionKind): string {
  return KIND_TEXT.get(kind) ?? kind;
}

/** A cost reads without its sign: money out is stored negative, "per month" is not. */
export function magnitude(amount: string): string {
  return isDecimalString(amount) && amountSign(amount) < 0 ? negateAmount(amount) : amount;
}

function cost(money: { amount: string; currency: string }): string | null {
  return formatMoney(magnitude(money.amount), money.currency);
}

function dateText(iso: string): string {
  return formatDate(iso) ?? iso;
}

/** One change of a series as a sentence fragment. */
export function eventText(event: Pick<SubscriptionEvent, "kind" | "occurred_on" | "amount_before" | "amount_after">): string {
  switch (event.kind) {
    case "price_changed": {
      const before = event.amount_before === null ? null : cost(event.amount_before);
      const after = event.amount_after === null ? null : cost(event.amount_after);
      return before === null || after === null ? "Price changed" : `Price went from ${before} to ${after}`;
    }
    case "missed":
      return `Expected on ${dateText(event.occurred_on)}, not seen`;
    case "ended":
      return "Stopped";
    case "new":
      return "New";
    case "charged_after_end":
      return "Charged after you ended it";
    case "renewal_due":
      return `Renews on ${dateText(event.occurred_on)}`;
    case "trial_converted":
      return "Trial became paid";
  }
}
