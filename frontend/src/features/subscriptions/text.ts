// SPDX-License-Identifier: AGPL-3.0-or-later
/** The words of the subscriptions view: cadence, status, kind and each change. */
import { formatDate } from "../../lib/dates";
import { amountSign, formatMoney, isDecimalString, negateAmount } from "../../lib/money";
import type { Subscription, SubscriptionEvent, SubscriptionKind, SubscriptionStatus } from "./api";

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

/** Income and anything else that repeats is neither of the two. */
const PLAIN_STATUS_TEXT = new Map<SubscriptionStatus, string>([
  ["suggested", "Might be recurring"],
  ["dismissed", "Not recurring"],
]);

const KIND_STATUS_TEXT = new Map<SubscriptionKind, Map<SubscriptionStatus, string>>([
  ["bill", BILL_STATUS_TEXT],
  ["income", PLAIN_STATUS_TEXT],
  ["other", PLAIN_STATUS_TEXT],
]);

/** The status in words; only a subscription is called one where the words name the kind. */
export function statusText(status: SubscriptionStatus, kind?: SubscriptionKind): string {
  return (kind === undefined ? undefined : KIND_STATUS_TEXT.get(kind)?.get(status)) ?? STATUS_TEXT.get(status) ?? status;
}

/** Who set a status or a kind: a person's decision sticks, so the view says whose it is. */
export function sourceText(source: Subscription["status_source"]): string {
  return source === "person" ? "set by you" : "set by PerFin";
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
export function eventText(event: EventWords): string {
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

type EventWords = Pick<SubscriptionEvent, "kind" | "occurred_on" | "amount_before" | "amount_after">;

/** One line of the price history: "<date>: <a> to <b>". */
export function priceLine(event: EventWords): string {
  const before = event.amount_before === null ? null : cost(event.amount_before);
  const after = event.amount_after === null ? null : cost(event.amount_after);
  return `${dateText(event.occurred_on)}: ${before === null || after === null ? "Price changed" : `${before} to ${after}`}`;
}

/** A change with its date in front, unless its own words already say the date. */
export function datedEventText(event: EventWords): string {
  const words = eventText(event);
  return event.kind === "missed" || event.kind === "renewal_due" ? words : `${dateText(event.occurred_on)}: ${words}`;
}
