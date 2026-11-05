// SPDX-License-Identifier: AGPL-3.0-or-later
import { fractionToPercentAmount, formatFraction, gainDisplay, multiplyAmounts, percentToFraction, ratioPercent, weightWidth } from "./format";

describe("percent helpers", () => {
  it("shifts fractions to percents exactly", () => {
    expect(fractionToPercentAmount("0.0825")).toBe("8.25");
    expect(fractionToPercentAmount("0.1")).toBe("10.00");
    expect(fractionToPercentAmount("1.5")).toBe("150.00");
    expect(fractionToPercentAmount("-0.000149", 2)).toBe("-0.01");
    expect(fractionToPercentAmount("abc")).toBeNull();
    expect(formatFraction("0.0825")).toBe("8.25%");
    expect(formatFraction(null)).toBe("n/a");
  });

  it("turns typed percents into API fractions", () => {
    expect(percentToFraction("6.125")).toBe("0.06125");
    expect(percentToFraction(" 5 ")).toBe("0.05");
    expect(percentToFraction("x")).toBeNull();
  });

  it("divides decimals with integer arithmetic", () => {
    expect(ratioPercent("125.50", "1000.00")).toBe("12.55");
    expect(ratioPercent("-40", "800")).toBe("-5.00");
    expect(ratioPercent("1", "3", 4)).toBe("33.3333");
    expect(ratioPercent("1", "0")).toBeNull();
    expect(multiplyAmounts("10.5", "3.333", 2)).toBe("35.00");
  });

  it("clamps bar widths", () => {
    expect(weightWidth("0.2534")).toBe("25.34%");
    expect(weightWidth("1.4")).toBe("100%");
    expect(weightWidth("-0.1")).toBe("0%");
    expect(weightWidth("nope")).toBe("0%");
  });
});

describe("gain display", () => {
  it("shows a gain with its percent of cost", () => {
    expect(gainDisplay("125.50", "1000.00")).toEqual({ amount: "125.50", percent: "12.55", tone: "gain", percentLabel: "+12.55%" });
  });

  it("shows a loss", () => {
    const view = gainDisplay("-40.00", "800.00");
    expect(view.tone).toBe("loss");
    expect(view.percentLabel).toBe("-5.00%");
  });

  it("copes with a zero cost and a flat gain", () => {
    expect(gainDisplay("10", "0")).toMatchObject({ percent: null, percentLabel: "n/a", tone: "gain" });
    expect(gainDisplay("0.00", "50").tone).toBe("flat");
  });
});
