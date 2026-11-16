// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What a subscription card says, in words: an open change of a series as a
 * sentence, and a suggested series as a question. Home's "Needs you" rows
 * say the same, so they import these. No React and no styles here, so Home's
 * chunk does not pull in the Inbox's cards.
 */
import { formatDate } from "../../lib/dates";
import { amountSign, formatMoney, isDecimalString, negateAmount } from "../../lib/money";
import type { Subscription, SubscriptionEvent } from "../subscriptions/api";

/** A cost without its sign: money out is stored negative, a price is not. */
export function cost(money: { amount: string; currency: string } | null): string | null {
  if (money === null) {
    return null;
  }
  const amount = isDecimalString(money.amount) && amountSign(money.amount) < 0 ? negateAmount(money.amount) : money.amount;
  return formatMoney(amount, money.currency);
}

function dateText(iso: string): string {
  return formatDate(iso) ?? iso;
}

/** One open change of a series, as a sentence. */
export function changeText(event: Pick<SubscriptionEvent, "kind" | "occurred_on" | "amount_before" | "amount_after">): string {
  switch (event.kind) {
    case "price_changed": {
      const before = cost(event.amount_before);
      const after = cost(event.amount_after);
      const words = before === null || after === null ? "Price changed" : `Price went from ${before} to ${after}`;
      return `${words} on ${dateText(event.occurred_on)}`;
    }
    case "missed":
      return `Expected on ${dateText(event.occurred_on)}, not seen`;
    case "ended":
      return `Stopped on ${dateText(event.occurred_on)}`;
    case "new":
      return `First seen on ${dateText(event.occurred_on)}`;
    case "charged_after_end":
      return `Charged after you ended it, on ${dateText(event.occurred_on)}`;
    case "renewal_due":
      return `Renews on ${dateText(event.occurred_on)}`;
    case "trial_converted":
      return `Trial became paid on ${dateText(event.occurred_on)}`;
  }
}

/** What PerFin thinks a suggested series is, as a question to answer. */
export function guessText(kind: Subscription["kind"]): string {
  if (kind === "bill") {
    return "Might be a bill";
  }
  return kind === "subscription" ? "Might be a subscription" : "Might be recurring";
}
