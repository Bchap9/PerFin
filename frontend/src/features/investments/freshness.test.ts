// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Holdings } from "./api";
import { freshnessNote, importRange, importedOn, priceSourceLabel, refreshEvery } from "./freshness";

const A = "a0000000-0000-4000-8000-0000000000a1";
const B = "a0000000-0000-4000-8000-0000000000a2";
const usd = (amount: string) => ({ amount, currency: "USD" });

function holdings(patch: Partial<Holdings> = {}): Holdings {
  return {
    as_of: "2026-11-12",
    base_currency: "USD",
    holdings: [],
    accounts: [
      { account_id: A, cost_method: "fifo", market_value_base: usd("1.00"), positions_as_of: "2026-11-11" },
      { account_id: B, cost_method: "fifo", market_value_base: usd("1.00"), positions_as_of: null },
    ],
    price_feed: { enabled: false, refresh_minutes: null },
    market_value: usd("2.00"),
    cost_basis: usd("2.00"),
    unrealized: { short_term: usd("0"), long_term: usd("0"), total: usd("0") },
    allocation: {},
    unpriced_security_ids: [],
    missing_rates: [],
    ...patch,
  };
}

const price = (patch: Record<string, unknown>) => ({
  close: "10",
  currency: "USD",
  price_date: "2026-11-12",
  basis: "price" as const,
  source: "manual" as string | null,
  updated_at: "2026-11-12T15:30:00Z" as string | null,
  ...patch,
});

describe("priceSourceLabel", () => {
  it("names a statement, a manual entry, a trade and a feed", () => {
    expect(priceSourceLabel(price({ source: "statement" }))).toBe("from an import");
    expect(priceSourceLabel(price({ source: "manual" }))).toBe("entered by hand");
    expect(priceSourceLabel(price({ source: "http_json" }))).toBe("price feed");
    expect(priceSourceLabel(price({ basis: "trade", source: null, updated_at: null }))).toBe("last trade");
    expect(priceSourceLabel(price({ source: null }))).toBe("stored price");
  });
});

describe("refreshEvery", () => {
  it("reads in minutes or whole hours", () => {
    expect(refreshEvery(5)).toBe("every 5 minutes");
    expect(refreshEvery(60)).toBe("every hour");
    expect(refreshEvery(360)).toBe("every 6 hours");
    expect(refreshEvery(90)).toBe("every 90 minutes");
  });
});

describe("what was imported when", () => {
  it("finds each account's last import and the range over all of them", () => {
    const data = holdings();
    expect(importedOn(data, A)).toBe("2026-11-11");
    expect(importedOn(data, B)).toBeNull();
    expect(importedOn(data, "unknown")).toBeNull();
    expect(importRange(data.accounts)).toEqual({ oldest: "2026-11-11", newest: "2026-11-11" });
    expect(importRange([])).toBeNull();
  });
});

describe("freshnessNote", () => {
  it("says nothing when no account was imported", () => {
    expect(freshnessNote(holdings({ accounts: [] }))).toBeNull();
  });

  it("names the last import and that values are estimates", () => {
    const note = freshnessNote(holdings());
    expect(note).toContain("as of your last import (Nov 11, 2026)");
    expect(note).toContain("No price feed is set up");
    expect(note).toContain("estimates");
  });

  it("names the range when accounts were imported on different days and the feed's pace", () => {
    const data = holdings({ price_feed: { enabled: true, refresh_minutes: 30 } });
    const [first, second] = data.accounts;
    const note = freshnessNote({
      ...data,
      accounts: [...(first ? [first] : []), ...(second ? [{ ...second, positions_as_of: "2026-10-31" }] : [])],
    });
    expect(note).toContain("each account's last import (Oct 31, 2026 to Nov 11, 2026)");
    expect(note).toContain("Prices are fetched every 30 minutes.");
  });
});
