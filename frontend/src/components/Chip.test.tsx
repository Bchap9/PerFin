// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../test/axe";
import { Chip } from "./Chip";

describe("Chip", () => {
  it("a pressable chip is a button with aria-pressed", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(
      <Chip pressed={false} onClick={onClick}>
        Uncategorised
      </Chip>,
    );
    const chip = screen.getByRole("button", { name: "Uncategorised" });
    expect(chip).toHaveAttribute("type", "button");
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(chip).toHaveClass("chip");
    await user.click(chip);
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(
      <Chip pressed onClick={onClick}>
        Uncategorised
      </Chip>,
    );
    expect(screen.getByRole("button", { name: "Uncategorised" })).toHaveAttribute("aria-pressed", "true");
  });

  it("a button without pressed has no aria-pressed", () => {
    render(<Chip onClick={() => undefined}>Groceries</Chip>);
    expect(screen.getByRole("button", { name: "Groceries" })).not.toHaveAttribute("aria-pressed");
  });

  it("without onClick it is a span with its tone and colour class", () => {
    render(
      <>
        <Chip tone="warn">Not checked</Chip>
        <Chip colourClass="chip--s2">Transport</Chip>
        <Chip>Plain</Chip>
      </>,
    );
    const warn = screen.getByText("Not checked").closest(".chip");
    expect(warn?.tagName).toBe("SPAN");
    expect(warn).toHaveClass("chip--warn");
    const transport = screen.getByText("Transport").closest(".chip");
    expect(transport).toHaveClass("chip--neutral", "chip--s2");
    // The colour class draws a dot; the dot is decorative.
    expect(transport?.querySelector(".chip__dot")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Plain").closest(".chip")?.querySelector(".chip__dot")).toBeNull();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = render(
      <p>
        <Chip tone="ok">Matched</Chip>
        <Chip tone="bad">Over</Chip>
        <Chip tone="info">Suggested</Chip>
        <Chip colourClass="chip--s1" pressed onClick={() => undefined}>
          Food
        </Chip>
      </p>,
    );
    await expectNoAxeViolations(container);
  });
});
