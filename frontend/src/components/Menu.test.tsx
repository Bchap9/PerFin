// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../test/axe";
import { Menu } from "./Menu";

function items(log: string[]) {
  return [
    { id: "edit", label: "Edit", onSelect: () => log.push("edit") },
    { id: "split", label: "Split", onSelect: () => log.push("split") },
    { id: "skip", label: "Skip for now", onSelect: () => log.push("skip"), disabled: true },
    { id: "delete", label: "Delete", onSelect: () => log.push("delete"), danger: true },
  ];
}

describe("Menu", () => {
  it("arrow keys move, Enter selects, Escape closes and returns focus", async () => {
    const user = userEvent.setup();
    const log: string[] = [];
    render(<Menu label="Actions for Coffee Corner" items={items(log)} />);
    const button = screen.getByRole("button", { name: "Actions for Coffee Corner" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");

    button.focus();
    await user.keyboard("{ArrowDown}");
    const menu = screen.getByRole("menu", { name: "Actions for Coffee Corner" });
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Split" })).toHaveFocus();
    // The disabled item is skipped.
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(menu).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    expect(log).toEqual([]);

    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    expect(log).toEqual(["split"]);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("typeahead", async () => {
    const user = userEvent.setup();
    const log: string[] = [];
    render(<Menu label="Row actions" items={items(log)} />);
    await user.click(screen.getByRole("button", { name: "Row actions" }));
    expect(screen.getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("d");
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("s");
    // "Skip for now" is disabled, so "s" lands on Split.
    expect(screen.getByRole("menuitem", { name: "Split" })).toHaveFocus();
  });

  it("places the open menu with the CSSOM, beside its button", async () => {
    const user = userEvent.setup();
    render(<Menu label="Row actions" items={items([])} trigger="More" />);
    const button = screen.getByRole("button", { name: "Row actions" });
    expect(button).toHaveTextContent("More");
    await user.click(button);
    const menu = screen.getByRole("menu");
    expect(menu.style.top).toMatch(/px$/);
    expect(menu.style.left).toMatch(/px$/);
  });

  it("has no axe violations", async () => {
    const user = userEvent.setup();
    const { container } = render(<Menu label="Row actions" items={items([])} />);
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "Row actions" }));
    await expectNoAxeViolations(container);
  });
});
