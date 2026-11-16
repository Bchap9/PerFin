// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * No page links to an old address (plan, decision 7). The redirects keep old
 * links working, but a link in our own code should name the page's one home.
 * Scans every page module under `src/features` (tests excluded: they visit
 * old addresses on purpose to check the redirects).
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { REDIRECTS } from "./routeMap";

const SRC = join(__dirname, "..");
const FEATURES = join(SRC, "features");

/**
 * Whether `target` starts with the route pattern `from`, segment by segment:
 * `/budgets` matches `/budgets`, `/budgets/b-1` and `/budgets?x=1`, not
 * `/budgets-old`; a `$param` segment matches any one segment.
 */
function startsWithRoute(target: string, from: string): boolean {
  const path = target.split(/[?#]/)[0] ?? "";
  const got = path.split("/");
  const want = from.split("/");
  return want.length <= got.length && want.every((part, index) => {
    const segment = got.at(index) ?? "";
    return part.startsWith("$") ? segment !== "" : part === segment;
  });
}

/** `to="/…"`, `to: "/…"`, `to={"/…"}`, `href="/…"` and the same with a template string. */
const LINK = /\b(?:to|href)\s*(?:=\s*\{?\s*|:\s*)(["'`])(\/[^"'`]*)\1?/g;

interface OldLink {
  line: number;
  target: string;
  from: string;
}

/** Every link in `source` whose target starts with an old address. */
function oldLinksIn(source: string): OldLink[] {
  const found: OldLink[] = [];
  source.split("\n").forEach((text, index) => {
    for (const match of text.matchAll(LINK)) {
      const target = match[2] ?? "";
      const old = REDIRECTS.find((row) => startsWithRoute(target, row.from));
      if (old !== undefined) {
        found.push({ line: index + 1, target, from: old.from });
      }
    }
  });
  return found;
}

function pageModules(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name))
    .map((entry) => join(entry.parentPath, entry.name));
}

describe("old links", () => {
  it("the matcher finds an old address and leaves its new home alone", () => {
    const sample = [
      '<Link to="/budgets">Budgets</Link>',
      '<Link to="/plan/budgets">Budgets</Link>',
      'navigate({ to: "/documents/$documentId", params })',
      '<a href="/review?document=d-1">x</a>',
      '<Link to="/inbox/documents">Documents</Link>',
      '<Link to="/taxes">not an old address</Link>',
      "<Link to={`/calendar`}>Upcoming</Link>",
    ].join("\n");
    expect(oldLinksIn(sample)).toEqual([
      { line: 1, target: "/budgets", from: "/budgets" },
      { line: 3, target: "/documents/$documentId", from: "/documents" },
      { line: 4, target: "/review?document=d-1", from: "/review" },
      { line: 7, target: "/calendar", from: "/calendar" },
    ]);
  });

  it("no page module links to an old address", () => {
    const files = pageModules(FEATURES);
    expect(files.length).toBeGreaterThan(50);
    const found = files.flatMap((file) =>
      oldLinksIn(readFileSync(file, "utf8")).map(
        (link) => `${relative(SRC, file)}:${String(link.line)} links to ${link.target} (moved from ${link.from})`,
      ),
    );
    expect(found).toEqual([]);
  });
});
