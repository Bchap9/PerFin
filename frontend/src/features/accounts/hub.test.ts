// SPDX-License-Identifier: AGPL-3.0-or-later
import { account } from "../transactions/fixtures";
import type { Account } from "./api";
import { defaultTab, HUB_TAB_IDS, tabsFor } from "./hub";

const ids = (extra: Partial<Account>, given: { loanId?: string; assetId?: string } = {}) =>
  tabsFor(account("a-1", "Example", extra), given).map((tab) => tab.id);

describe("account hub tabs", () => {
  it("opens on the overview", () => {
    expect(defaultTab).toBe("overview");
  });

  it("gives every type its overview, transactions and documents", () => {
    for (const type of ["checking", "savings", "cash", "credit_card", "manual_liability"] as const) {
      expect(ids({ type })).toEqual(["overview", "transactions", "documents"]);
    }
  });

  it("adds holdings, activity and performance to an investment account", () => {
    for (const type of ["brokerage", "retirement"] as const) {
      expect(ids({ type })).toEqual(["overview", "transactions", "documents", "holdings", "activity", "performance"]);
    }
  });

  it("adds the four loan tabs to a loan or mortgage with terms", () => {
    for (const type of ["loan", "mortgage"] as const) {
      expect(ids({ type }, { loanId: "l-1" })).toEqual(["overview", "transactions", "documents", "schedule", "payments", "rates", "payoff"]);
    }
  });

  it("offers a loan or mortgage without terms only the schedule, where the terms are added", () => {
    expect(ids({ type: "mortgage" })).toEqual(["overview", "transactions", "documents", "schedule"]);
  });

  it("gives any account that carries a loan its loan tabs, such as a line of credit", () => {
    expect(ids({ type: "line_of_credit" }, { loanId: "l-1" })).toEqual(["overview", "transactions", "documents", "schedule", "payments", "rates", "payoff"]);
    expect(ids({ type: "line_of_credit" })).toEqual(["overview", "transactions", "documents"]);
  });

  it("adds valuations to a manual asset, with or without its details", () => {
    expect(ids({ type: "manual_asset" }, { assetId: "x-1" })).toEqual(["overview", "transactions", "documents", "valuations"]);
    expect(ids({ type: "manual_asset" })).toEqual(["overview", "transactions", "documents", "valuations"]);
  });

  it("labels each tab in words", () => {
    const labels = tabsFor(account("a-1", "Example", { type: "mortgage" }), { loanId: "l-1" }).map((tab) => tab.label);
    expect(labels).toEqual(["Overview", "Transactions", "Documents", "Schedule", "Payments", "Rate changes", "Payoff"]);
  });

  it("knows every id the address may carry", () => {
    expect(HUB_TAB_IDS).toEqual([
      "overview",
      "transactions",
      "documents",
      "holdings",
      "activity",
      "performance",
      "schedule",
      "payments",
      "rates",
      "payoff",
      "valuations",
    ]);
  });
});
