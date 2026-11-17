// SPDX-License-Identifier: AGPL-3.0-or-later
import { titleCase } from "./parts";

describe("titleCase", () => {
  it("spells an asset class as a label", () => {
    expect(titleCase("mutual_fund")).toBe("Mutual fund");
    expect(titleCase("equity")).toBe("Equity");
  });

  it("keeps an acronym in capitals", () => {
    expect(titleCase("etf")).toBe("ETF");
  });
});
