// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen, within } from "@testing-library/react";

import { expectNoAxeViolations } from "../test/axe";
import { ProgressBar } from "./ProgressBar";
import { QuietNote } from "./QuietNote";
import { Sparkline } from "./Sparkline";
import { StackBar } from "./StackBar";

describe("ProgressBar", () => {
  it("reports its value and draws the width as an attribute", () => {
    const { container } = render(<ProgressBar value={45} max={60} label="Groceries, $45 of $60" />);
    const bar = screen.getByRole("progressbar", { name: "Groceries, $45 of $60" });
    expect(bar).toHaveAttribute("aria-valuenow", "45");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "60");
    const fill = container.querySelector(".progress__value");
    expect(fill).toHaveAttribute("width", "75");
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });

  it("clamps the width at 100 and marks over", () => {
    const { container } = render(<ProgressBar value={90} max={60} label="Dining" over />);
    expect(screen.getByRole("progressbar", { name: "Dining" })).toHaveAttribute("aria-valuenow", "90");
    expect(container.querySelector(".progress__value")).toHaveAttribute("width", "100");
    expect(container.querySelector(".progress")).toHaveClass("progress--over");
  });

  it("draws nothing for a zero or broken maximum", () => {
    const { container } = render(<ProgressBar value={5} max={0} label="Empty" />);
    expect(container.querySelector(".progress__value")).toHaveAttribute("width", "0");
    const { container: nan } = render(<ProgressBar value={Number.NaN} max={10} label="Broken" />);
    expect(nan.querySelector(".progress__value")).toHaveAttribute("width", "0");
  });
});

describe("StackBar", () => {
  const parts = [
    { label: "Food", value: 300, colourClass: "chip--s1" },
    { label: "Transport", value: 100, colourClass: "chip--s2" },
    { label: "Leisure", value: 100, colourClass: "chip--s3" },
  ];

  it("has a legend with every part", () => {
    const { container } = render(<StackBar label="Spending by group" parts={parts} />);
    expect(screen.getByRole("img", { name: "Spending by group" })).toBeInTheDocument();
    const legend = screen.getByRole("list", { name: "Spending by group" });
    const entries = within(legend).getAllByRole("listitem");
    expect(entries.map((entry) => entry.textContent)).toEqual(["Food300", "Transport100", "Leisure100"]);
    const rects = Array.from(container.querySelectorAll("rect.stack__part"));
    expect(rects.map((rect) => rect.getAttribute("width"))).toEqual(["60", "20", "20"]);
    expect(rects.map((rect) => rect.getAttribute("x"))).toEqual(["0", "60", "80"]);
    expect(rects[0]).toHaveClass("chip--s1");
  });

  it("formats the amounts when asked", () => {
    render(<StackBar label="Split" parts={parts} format={(value) => `$${String(value)}`} />);
    expect(screen.getByText("$300")).toBeInTheDocument();
  });

  it("an empty bar draws no parts", () => {
    const { container } = render(<StackBar label="Nothing" parts={[{ label: "Food", value: 0, colourClass: "chip--s1" }]} />);
    expect(container.querySelectorAll("rect.stack__part")).toHaveLength(0);
  });
});

describe("Sparkline", () => {
  it("has a path and its label", () => {
    const { container } = render(<Sparkline points={[10, 20, 15, 30]} label="Net worth over 4 months, from $10 to $30" />);
    const image = screen.getByRole("img", { name: "Net worth over 4 months, from $10 to $30" });
    expect(image).toHaveAttribute("viewBox", "0 0 300 120");
    expect(image).toHaveAttribute("preserveAspectRatio", "none");
    const line = container.querySelector("path.sparkline__line");
    const area = container.querySelector("path.sparkline__area");
    expect(line?.getAttribute("d")).toMatch(/^M0[ ,]/);
    expect(area?.getAttribute("d")).toMatch(/Z$/);
  });

  it("draws a flat line for one point and nothing for none", () => {
    const { container } = render(<Sparkline points={[5]} label="One" />);
    expect(container.querySelector("path.sparkline__line")).not.toBeNull();
    const { container: none } = render(<Sparkline points={[]} label="None" />);
    expect(none.querySelector("path.sparkline__line")).toBeNull();
  });
});

describe("QuietNote", () => {
  it("is muted text with a dot", () => {
    render(<QuietNote>Not checked against a statement</QuietNote>);
    expect(screen.getByText("Not checked against a statement")).toHaveClass("quiet");
  });
});

describe("graphics accessibility", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <div>
        <ProgressBar value={45} max={60} label="Groceries" />
        <StackBar label="Spending" parts={[{ label: "Food", value: 3, colourClass: "chip--s1" }]} />
        <Sparkline points={[1, 3, 2]} label="Balance" />
        <QuietNote>Estimate</QuietNote>
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});
