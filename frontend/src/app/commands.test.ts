// SPDX-License-Identifier: AGPL-3.0-or-later
import { accountCommands, rank, staticCommands, type Command } from "./commands";

function cmd(label: string): Command {
  return { id: label, label, to: "/" };
}

const labels = (commands: readonly Command[]) => commands.map((command) => command.label);

describe("staticCommands", () => {
  it("lists every section, tab and settings page of the route map", () => {
    const all = labels(staticCommands(false));
    expect(all.slice(0, 6)).toEqual(["Home", "Inbox", "Transactions", "Accounts", "Plan", "Reports"]);
    expect(all).toContain("Inbox: Documents");
    expect(all).toContain("Plan: Budgets");
    expect(all).toContain("Reports: Net worth");
    expect(all).toContain("Settings");
    expect(all).toContain("Settings: Rules");
    expect(all).toContain("Settings: What PerFin learned");
  });

  it("carries a tab's search", () => {
    const income = staticCommands(false).find((command) => command.label === "Reports: Income");
    expect(income).toMatchObject({ to: "/reports", search: { tab: "income" } });
  });

  it("lists instance pages only for an instance administrator", () => {
    expect(labels(staticCommands(false))).not.toContain("Settings: Users");
    expect(labels(staticCommands(true))).toContain("Settings: Users");
  });

  it("gives every command its own id", () => {
    const ids = staticCommands(true).map((command) => command.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("accountCommands", () => {
  it("names each account and goes to its page", () => {
    const commands = accountCommands([
      { id: "acc-1", name: "Everyday checking", nickname: null },
      { id: "acc-2", name: "Card 1234", nickname: "Travel card" },
    ]);
    expect(commands).toEqual([
      { id: "account:acc-1", label: "Account: Everyday checking", to: "/accounts/$accountId", params: { accountId: "acc-1" } },
      { id: "account:acc-2", label: "Account: Travel card", to: "/accounts/$accountId", params: { accountId: "acc-2" } },
    ]);
  });
});

describe("rank", () => {
  it("puts prefix matches above word-start matches above substring matches", () => {
    // Each better match comes later in the input, so only the tiers can reorder them.
    const commands = [cmd("Plain beans"), cmd("Syntax"), cmd("Reports: Tax"), cmd("Tax years")];
    expect(labels(rank(commands, "tax"))).toEqual(["Tax years", "Reports: Tax", "Syntax"]);
  });

  it("keeps the given order inside a tier and drops non-matches", () => {
    const commands = [cmd("Budget B"), cmd("Budget A"), cmd("Goals")];
    expect(labels(rank(commands, "budg"))).toEqual(["Budget B", "Budget A"]);
  });

  it("ignores case and accents, both ways", () => {
    const commands = [cmd("Café savings"), cmd("Resume"), cmd("Other")];
    expect(labels(rank(commands, "CAFE"))).toEqual(["Café savings"]);
    expect(labels(rank(commands, "résumé"))).toEqual(["Resume"]);
  });

  it("returns at most 12, also for an empty query", () => {
    const many = Array.from({ length: 20 }, (_, index) => cmd(`Account ${String(index)}`));
    expect(rank(many, "account")).toHaveLength(12);
    expect(labels(rank(many, "  "))).toEqual(labels(many.slice(0, 12)));
  });

  it("treats the query as text, not a pattern", () => {
    const commands = [cmd("Cash (main)"), cmd("Cash main")];
    expect(labels(rank(commands, "(main"))).toEqual(["Cash (main)"]);
    expect(labels(rank(commands, ".*"))).toEqual([]);
  });
});
