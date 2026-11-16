// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Accessibility check for component and page tests: runs axe-core on a
 * rendered container and fails with each violation's rule id, help text and
 * first three targets.
 *
 * Colour contrast is off here (jsdom computes no styles); the token pairs are
 * checked by app/styles/contrast.test.ts instead. The `region` rule is off
 * unless the whole document is checked, since a fragment has no landmarks.
 */
import axe from "axe-core";

export async function expectNoAxeViolations(
  container: Element,
  options?: { rules?: Record<string, { enabled: boolean }> },
): Promise<void> {
  const wholeDocument = container === document.documentElement;
  const rules: Record<string, { enabled: boolean }> = {
    "color-contrast": { enabled: false },
    ...(wholeDocument ? {} : { region: { enabled: false } }),
    ...options?.rules,
  };
  const results = await axe.run(container, { rules });
  if (results.violations.length === 0) return;
  const lines = results.violations.map((violation) => {
    const targets = violation.nodes
      .slice(0, 3)
      .map((node) => node.target.map(String).join(" "))
      .join(", ");
    return `${violation.id}: ${violation.help} (${targets})`;
  });
  throw new Error(`axe found ${String(results.violations.length)} violation(s):\n${lines.join("\n")}`);
}
