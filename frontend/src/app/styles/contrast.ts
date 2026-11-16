// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Contrast checks for the design tokens (WCAG 2.2, 1.4.3 and 1.4.11).
 *
 * `parseThemes` reads tokens.css as text: the light `:root` block, the dark
 * block inside `@media (prefers-color-scheme: dark)` and the forced dark
 * `:root[data-theme="dark"]` block. Each map holds what that block declares,
 * with `var(--x)` resolved inside the same block first, then against the
 * light block (the cascade a dark block sits on). Only `#rrggbb` values are
 * compared by `ratio`.
 */

export interface Themes {
  light: Map<string, string>;
  dark: Map<string, string>;
  darkForced: Map<string, string>;
}

export interface Pair {
  fg: string;
  bg: string;
  min: number;
}

const TEXT = 4.5;
const UI = 3;

function text(fg: string, ...bgs: string[]): Pair[] {
  return bgs.map((bg) => ({ fg: `--color-${fg}`, bg: `--color-${bg}`, min: TEXT }));
}

function ui(fg: string, ...bgs: string[]): Pair[] {
  return bgs.map((bg) => ({ fg: `--color-${fg}`, bg: `--color-${bg}`, min: UI }));
}

export const PAIRS: readonly Pair[] = [
  ...text("text", "bg", "surface", "surface-sunken"),
  ...text("text-muted", "bg", "surface", "surface-sunken"),
  ...text("accent", "surface", "accent-soft"),
  ...text("accent-contrast", "accent"),
  ...text("warning", "warning-soft", "surface"),
  ...text("danger", "danger-soft", "surface"),
  ...text("info", "info-soft", "surface"),
  ...text("text", "accent-soft"),
  ...ui("border-strong", "surface", "bg"),
  ...ui("focus", "surface", "bg"),
  ...[1, 2, 3, 4, 5, 6].flatMap((n) => ui(`series-${String(n)}`, "surface")),
];

/** The body of the block whose opening brace follows `from`, or undefined. */
function blockBody(css: string, from: number): { body: string; end: number } | undefined {
  const open = css.indexOf("{", from);
  if (open === -1) return undefined;
  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    const char = css.charAt(index);
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return { body: css.slice(open + 1, index), end: index + 1 };
    }
  }
  return undefined;
}

function declarations(body: string | undefined): Map<string, string> {
  const out = new Map<string, string>();
  if (body === undefined) return out;
  for (const match of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined) out.set(name, value.trim());
  }
  return out;
}

function resolve(own: Map<string, string>, fallback: Map<string, string>): Map<string, string> {
  const lookup = (name: string, seen: Set<string>): string | undefined => {
    if (seen.has(name)) return undefined;
    seen.add(name);
    const value = own.get(name) ?? fallback.get(name);
    if (value === undefined) return undefined;
    const reference = /^var\(\s*(--[\w-]+)\s*\)$/.exec(value);
    if (reference?.[1] === undefined) return value;
    // Custom properties resolve where they are used, so a reference reads the
    // block's own value before the light one, as the cascade does.
    return lookup(reference[1], seen);
  };
  const out = new Map<string, string>();
  for (const name of own.keys()) {
    const value = lookup(name, new Set());
    if (value !== undefined) out.set(name, value);
  }
  return out;
}

export function parseThemes(source: string): Themes {
  const css = source.replace(/\/\*[\s\S]*?\*\//g, "");

  const lightAt = /(?:^|\})\s*:root\s*\{/.exec(css);
  const light = declarations(lightAt === null ? undefined : blockBody(css, lightAt.index)?.body);

  let darkBody: string | undefined;
  const mediaAt = /@media\s*\(\s*prefers-color-scheme:\s*dark\s*\)\s*\{/.exec(css);
  if (mediaAt !== null) {
    const media = blockBody(css, mediaAt.index)?.body ?? "";
    const innerAt = /:root:not\(\[data-theme="light"\]\)\s*\{/.exec(media);
    darkBody = innerAt === null ? undefined : blockBody(media, innerAt.index)?.body;
  }

  const forcedAt = /:root\[data-theme="dark"\]\s*\{/.exec(css);
  const forcedBody = forcedAt === null ? undefined : blockBody(css, forcedAt.index)?.body;

  return {
    light: resolve(light, new Map()),
    dark: resolve(declarations(darkBody), light),
    darkForced: resolve(declarations(forcedBody), light),
  };
}

function channel(hex: string): number {
  const value = Number.parseInt(hex, 16) / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function luminance(colour: string): number {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(colour.trim());
  if (match === null) throw new Error(`not a #rrggbb colour: ${colour}`);
  const [, r = "", g = "", b = ""] = match;
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG 2.2 contrast ratio between two `#rrggbb` colours. */
export function ratio(foreground: string, background: string): number {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
