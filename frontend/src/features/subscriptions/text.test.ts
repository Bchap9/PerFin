// SPDX-License-Identifier: AGPL-3.0-or-later
import type { SubscriptionEvent } from "./api";
import { cadenceText, eventText, kindText, magnitude, statusText } from "./text";

function event(kind: SubscriptionEvent["kind"], extra: Partial<SubscriptionEvent> = {}): SubscriptionEvent {
  return {
    id: "e-1",
    schedule_id: "s-1",
    name: "Streamly",
    kind,
    state: "open",
    occurred_on: "2026-11-10",
    amount_before: null,
    amount_after: null,
    transaction_id: null,
    ...extra,
  };
}

describe("subscription words", () => {
  it("says how often a series repeats", () => {
    expect(cadenceText("monthly", 1)).toBe("Every month");
    expect(cadenceText("monthly", 2)).toBe("Every 2 months");
    expect(cadenceText("yearly", 1)).toBe("Every year");
    expect(cadenceText("weekly", 2)).toBe("Every 2 weeks");
  });

  it("names each status", () => {
    expect(statusText("suggested")).toBe("Might be a subscription");
    expect(statusText("active")).toBe("Found for you");
    expect(statusText("confirmed")).toBe("Confirmed");
    expect(statusText("paused")).toBe("Paused");
    expect(statusText("ended")).toBe("Ended");
    expect(statusText("dismissed")).toBe("Not a subscription");
    expect(statusText("suggested", "subscription")).toBe("Might be a subscription");
    expect(statusText("suggested", "bill")).toBe("Might be a bill");
    expect(statusText("dismissed", "bill")).toBe("Not a bill");
    expect(statusText("confirmed", "bill")).toBe("Confirmed");
  });

  it("names each kind", () => {
    expect(kindText("subscription")).toBe("Subscription");
    expect(kindText("bill")).toBe("Bill");
    expect(kindText("income")).toBe("Income");
    expect(kindText("other")).toBe("Other");
  });

  it("puts each change in words", () => {
    expect(
      eventText(
        event("price_changed", {
          amount_before: { amount: "-15.99", currency: "USD" },
          amount_after: { amount: "-17.99", currency: "USD" },
        }),
      ),
    ).toBe("Price went from $15.99 to $17.99");
    expect(eventText(event("missed"))).toBe("Expected on Nov 10, 2026, not seen");
    expect(eventText(event("ended"))).toBe("Stopped");
    expect(eventText(event("new"))).toBe("New");
    expect(eventText(event("charged_after_end"))).toBe("Charged after you ended it");
    expect(eventText(event("renewal_due"))).toBe("Renews on Nov 10, 2026");
    expect(eventText(event("trial_converted"))).toBe("Trial became paid");
  });

  it("a price change without both amounts is still said", () => {
    expect(eventText(event("price_changed"))).toBe("Price changed");
    expect(eventText(event("price_changed", { amount_after: { amount: "-17.99", currency: "USD" } }))).toBe("Price changed");
  });

  it("a cost is shown without its sign", () => {
    expect(magnitude("-15.99")).toBe("15.99");
    expect(magnitude("15.99")).toBe("15.99");
    expect(magnitude("0.00")).toBe("0.00");
    expect(magnitude("not a number")).toBe("not a number");
  });
});
