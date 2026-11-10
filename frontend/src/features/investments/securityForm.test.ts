// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Security } from "./api";
import { buildSecurityCreate, buildSecurityUpdate, securityFormOf } from "./securityForm";

const STAMP = "2026-11-01T10:00:00Z";

function security(patch: Partial<Security> = {}): Security {
  return {
    id: "s-1",
    symbol: "ACME",
    name: "Acme Corp",
    asset_class: "equity",
    currency: "USD",
    isin: null,
    cusip: null,
    sector: "technology",
    exchange: "NYSE",
    created_at: STAMP,
    updated_at: STAMP,
    ...patch,
  };
}

describe("security form", () => {
  it("builds a new security with an upper-cased symbol and only the optional fields given", () => {
    const form = { ...securityFormOf(undefined, "USD"), symbol: " acme ", name: " Acme Corp ", isin: "us0378331005" };
    expect(buildSecurityCreate(form)).toEqual({
      ok: true,
      body: { symbol: "ACME", name: "Acme Corp", asset_class: "equity", currency: "USD", isin: "US0378331005" },
    });
  });

  it("names what is wrong with a new security", () => {
    const result = buildSecurityCreate({ ...securityFormOf(undefined, "US"), symbol: "A B", cusip: "12" });
    expect(result).toEqual({
      ok: false,
      problems: {
        symbol: "Use up to 32 letters, digits and . ^ / : _ - without spaces.",
        name: "Enter a name.",
        currency: "Enter a three-letter currency code.",
        cusip: "A CUSIP has 9 characters.",
      },
    });
  });

  it("sends nothing when an edit changes nothing, and only the differences otherwise", () => {
    const original = security();
    expect(buildSecurityUpdate(securityFormOf(original, "USD"), original)).toEqual({ ok: true, body: {} });
    const changed = { ...securityFormOf(original, "USD"), name: "Acme Corporation", assetClass: "etf" as const, sector: " ", exchange: "Nasdaq", cusip: "037833100" };
    expect(buildSecurityUpdate(changed, original)).toEqual({
      ok: true,
      body: { name: "Acme Corporation", asset_class: "etf", sector: null, exchange: "Nasdaq", cusip: "037833100" },
    });
  });

  it("keeps a stored symbol as it is, upper-cases a new one and refuses removing one", () => {
    const lower = security({ symbol: "brk.b" });
    expect(buildSecurityUpdate(securityFormOf(lower, "USD"), lower)).toEqual({ ok: true, body: {} });
    expect(buildSecurityUpdate({ ...securityFormOf(lower, "USD"), symbol: "brk.a" }, lower)).toEqual({ ok: true, body: { symbol: "BRK.A" } });
    expect(buildSecurityUpdate({ ...securityFormOf(lower, "USD"), symbol: "" }, lower)).toEqual({
      ok: false,
      problems: { symbol: "Enter the ticker symbol." },
    });
  });

  it("lets a security imported by name stay without a symbol or gain one", () => {
    const named = security({ symbol: null, name: "Example Plan Portfolio" });
    expect(buildSecurityUpdate({ ...securityFormOf(named, "USD"), name: "Example Plan" }, named)).toEqual({ ok: true, body: { name: "Example Plan" } });
    expect(buildSecurityUpdate({ ...securityFormOf(named, "USD"), symbol: "expl" }, named)).toEqual({ ok: true, body: { symbol: "EXPL" } });
  });
});
