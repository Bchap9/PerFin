// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Scrubber placeholders in text. The backend scrubber (app/core/scrubber.py)
 * replaces identity data with these typed tokens. The page highlights them by
 * splitting the text into segments that React renders as text nodes inside
 * styled spans. The text is never parsed as HTML and never reaches innerHTML.
 */

export const PLACEHOLDERS = ["[ACCT]", "[GOVID]", "[EMAIL]", "[PHONE]", "[ADDRESS]", "[MEMBER]", "[REDACTED]"] as const;

export type Placeholder = (typeof PLACEHOLDERS)[number];

export type Segment = { readonly kind: "text"; readonly text: string } | { readonly kind: "placeholder"; readonly text: Placeholder };

const PLACEHOLDER_SET: ReadonlySet<string> = new Set(PLACEHOLDERS);

function isPlaceholder(value: string): value is Placeholder {
  return PLACEHOLDER_SET.has(value);
}

/** Split `text` into plain and placeholder segments. Joining the segments gives `text` back. */
export function splitPlaceholders(text: string): Segment[] {
  const segments: Segment[] = [];
  let rest = text;
  while (rest.length > 0) {
    let at = -1;
    let found: Placeholder | null = null;
    for (const placeholder of PLACEHOLDERS) {
      const index = rest.indexOf(placeholder);
      if (index !== -1 && (at === -1 || index < at)) {
        at = index;
        found = placeholder;
      }
    }
    if (found === null) {
      segments.push({ kind: "text", text: rest });
      break;
    }
    if (at > 0) {
      segments.push({ kind: "text", text: rest.slice(0, at) });
    }
    segments.push({ kind: "placeholder", text: found });
    rest = rest.slice(at + found.length);
  }
  return segments;
}

/** How many placeholders each kind has in `text`. */
export function countPlaceholders(text: string): Partial<Record<Placeholder, number>> {
  const counts: Partial<Record<Placeholder, number>> = {};
  for (const segment of splitPlaceholders(text)) {
    if (segment.kind === "placeholder" && isPlaceholder(segment.text)) {
      counts[segment.text] = (counts[segment.text] ?? 0) + 1;
    }
  }
  return counts;
}
