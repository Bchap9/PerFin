// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen, within } from "@testing-library/react";

import { expectNoAxeViolations } from "../test/axe";
import { Stat, StatGroup } from "./Stat";

function Figures() {
  return (
    <StatGroup label="This month">
      <Stat label="Net worth" value="$12,400.00" size="display" change={{ text: "up 8%", tone: "up" }} />
      <Stat label="Spent" value="$1,020.00" change={{ text: "down 3%", tone: "down" }} />
      <Stat label="Saved" value="$300.00" change={{ text: "same as last month", tone: "neutral" }} />
      <Stat label="Accounts" value="4" />
    </StatGroup>
  );
}

describe("Stat", () => {
  it("is a description list of label and value", () => {
    const { container } = render(<Figures />);
    const list = container.querySelector("dl");
    expect(list).not.toBeNull();
    expect(list).toHaveClass("stat-group");
    expect(list).toHaveAttribute("aria-label", "This month");
    const terms = Array.from(container.querySelectorAll("dt")).map((term) => term.textContent);
    expect(terms).toEqual(["Net worth", "Spent", "Saved", "Accounts"]);
    const values = container.querySelectorAll("dd.stat__value");
    expect(values[0]).toHaveTextContent("$12,400.00");
    expect(values[0]?.closest(".stat")).toHaveClass("stat--display");
  });

  it("the change tone is in the text, not only in colour", () => {
    render(<Figures />);
    const up = screen.getByText("up 8%");
    expect(up).toHaveClass("stat__change", "stat__change--up");
    expect(screen.getByText("down 3%")).toHaveClass("stat__change--down");
    expect(screen.getByText("same as last month")).toHaveClass("stat__change--neutral");
    const accounts = screen.getByText("Accounts").closest(".stat");
    expect(accounts).not.toBeNull();
    expect(within(accounts as HTMLElement).queryByText(/up|down/)).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = render(<Figures />);
    await expectNoAxeViolations(container);
  });
});
