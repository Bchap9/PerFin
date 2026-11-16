// SPDX-License-Identifier: AGPL-3.0-or-later
import { render } from "@testing-library/react";

import { expectNoAxeViolations } from "./axe";

describe("expectNoAxeViolations", () => {
  it("passes a labelled button", async () => {
    const { container } = render(<button type="button">Save</button>);
    await expectNoAxeViolations(container);
  });

  it("fails an unlabelled icon button and names the rule", async () => {
    const { container } = render(
      <button type="button">
        <svg aria-hidden="true" viewBox="0 0 10 10" />
      </button>,
    );
    await expect(expectNoAxeViolations(container)).rejects.toThrow(/button-name/);
  });
});
