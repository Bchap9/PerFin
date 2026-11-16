// SPDX-License-Identifier: AGPL-3.0-or-later
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { PAIRS, parseThemes, ratio } from "./contrast";

const css = readFileSync(join(import.meta.dirname, "tokens.css"), "utf8");
const themes = parseThemes(css);

function failures(theme: Map<string, string>): string[] {
  const out: string[] = [];
  for (const pair of PAIRS) {
    const fg = theme.get(pair.fg);
    const bg = theme.get(pair.bg);
    if (fg === undefined || bg === undefined) {
      out.push(`${pair.fg} on ${pair.bg}: missing`);
      continue;
    }
    const value = ratio(fg, bg);
    if (value < pair.min) {
      out.push(`${pair.fg} (${fg}) on ${pair.bg} (${bg}): ${value.toFixed(2)} < ${String(pair.min)}`);
    }
  }
  return out;
}

describe("contrast helpers", () => {
  it("computes the WCAG ratio", () => {
    expect(ratio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(ratio("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    expect(ratio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  it("resolves var() inside a theme", () => {
    const parsed = parseThemes(`
      :root { --a: #111111; --b: var(--a); }
      @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --a: #eeeeee; --b: var(--a); } }
      :root[data-theme="dark"] { --a: #eeeeee; --b: var(--a); }
    `);
    expect(parsed.light.get("--b")).toBe("#111111");
    expect(parsed.dark.get("--b")).toBe("#eeeeee");
    expect(parsed.darkForced.get("--b")).toBe("#eeeeee");
  });
});

describe("theme tokens", () => {
  it("each pair meets its minimum in the light theme", () => {
    expect(failures(themes.light)).toEqual([]);
  });

  it("each pair meets its minimum in the dark theme (media block)", () => {
    expect(failures(themes.dark)).toEqual([]);
  });

  it("each pair meets its minimum in the dark theme (forced block)", () => {
    expect(failures(themes.darkForced)).toEqual([]);
  });

  it("the two dark blocks declare the same values", () => {
    expect(themes.dark.size).toBeGreaterThan(0);
    expect(Object.fromEntries(themes.darkForced)).toEqual(Object.fromEntries(themes.dark));
  });

  it("every token named in a pair exists in all three blocks", () => {
    const names = new Set(PAIRS.flatMap((pair) => [pair.fg, pair.bg]));
    const missing: string[] = [];
    for (const [themeName, theme] of [
      ["light", themes.light],
      ["dark", themes.dark],
      ["darkForced", themes.darkForced],
    ] as const) {
      for (const name of names) {
        if (!theme.has(name)) missing.push(`${themeName}: ${name}`);
      }
    }
    expect(missing).toEqual([]);
  });
});
