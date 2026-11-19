// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Bundle budget (spec 2026-11-16-ui-redesign-design.md, section 10), checked
 * after `npm run build`: `node scripts/check-bundle.mjs [distDir]`.
 *
 * It reads `dist/index.html` for the entry script, its module preloads and
 * the stylesheets (no Vite manifest needed), gzips every file at level 9 and
 * compares the sums with BUDGET. It exits 1 with a table of measured against
 * budget when any figure is over. Budgets are set before measuring: an
 * overrun is reported to the coordinator with the figures, never fixed by
 * raising a number here.
 *
 * Every figure covers the whole of `dist` (the service worker `sw.js` at
 * its root included), as `measure` walks it.
 *
 * Baseline: on 2026-11-16, at a9f237b (the stage 2 head plus the Start
 * tasks 1 to 4, before route splitting), the build had one app JS file of
 * 1,391,088 bytes, 390,815 bytes gzip (level 9), plus `sw.js` at 1,169
 * bytes gzip: 391,984 in all. CSS was one file of 11,043 bytes gzip. The
 * Latin font files were 61,504 bytes gzip (Figtree 20,127 and Bricolage
 * Grotesque 41,377).
 *
 * Route splitting (Task 5) cut the first load from 398 kB to 151 kB gzip
 * but raised the total to 447,495 bytes gzip (61 files compressed apart;
 * raw app JS grew 3.3%). The coordinator re-based the total on that split
 * build, so the 5% allowance measures what the section lanes add, not
 * the cost of splitting.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";

/** All JS in `dist`, gzip, after Task 5 split the routes (c53eb9b). */
export const BASELINE_TOTAL_JS = 447_495;

/** Bytes, gzip. 1 kB = 1,000 bytes, as Vite reports. */
export const BUDGET = Object.freeze({
  /** The entry and its static imports: what `/` needs before it can render. */
  initialJs: 250_000,
  largestLazyJs: 160_000,
  /**
   * The Start baseline plus 5%, plus 512 bytes the coordinator allowed in
   * the release review: per-page document titles (WCAG 2.4.2), the Inbox's
   * words for an opening balance that was not recorded, and the ~110 bytes
   * a CI build measures above a macOS build of the same tree. Another 1,024
   * bytes the owner allowed for rc.16: the review card that shows the lines a
   * table import skipped, as the file prints them.
   */
  totalJs: Math.floor(BASELINE_TOTAL_JS * 1.05) + 1536,
  totalCss: 20_000,
  /** The Latin `woff2` files a page loads. */
  fonts: 120_000,
});

const LABELS = {
  initialJs: "JS needed for / (entry and preloads)",
  largestLazyJs: "Largest lazy JS chunk",
  totalJs: "All JS",
  totalCss: "All CSS",
  fonts: "Latin font files (woff2)",
};

function gzipSize(path) {
  return gzipSync(readFileSync(path), { level: 9 }).length;
}

function walk(dir) {
  const files = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      files.push(...walk(path));
    } else {
      files.push(path);
    }
  }
  return files;
}

/** Paths that index.html names in an attribute matching `pattern` (resolved against distDir). */
function referenced(html, distDir, pattern) {
  const paths = [];
  for (const match of html.matchAll(pattern)) {
    const href = match[1];
    if (href.startsWith("/") && !href.startsWith("//")) {
      paths.push(resolve(distDir, `.${href}`));
    }
  }
  return paths;
}

/** Gzip sizes of the built app in `distDir`. */
export function measure(distDir) {
  const root = resolve(distDir);
  const html = readFileSync(join(root, "index.html"), "utf8");
  const entries = referenced(html, root, /<script\b[^>]*\btype="module"[^>]*\bsrc="([^"]+)"/g);
  const preloads = referenced(html, root, /<link\b[^>]*\brel="modulepreload"[^>]*\bhref="([^"]+)"/g);
  const initial = new Set([...entries, ...preloads]);

  const files = walk(root);
  const js = files.filter((path) => path.endsWith(".js"));
  const sizes = new Map(js.map((path) => [path, gzipSize(path)]));
  let initialJs = 0;
  let totalJs = 0;
  let largestLazyJs = 0;
  for (const [path, size] of sizes) {
    totalJs += size;
    if (initial.has(path)) {
      initialJs += size;
    } else if (size > largestLazyJs) {
      largestLazyJs = size;
    }
  }
  const totalCss = files.filter((path) => path.endsWith(".css")).reduce((sum, path) => sum + gzipSize(path), 0);
  const fonts = files
    .filter((path) => path.endsWith(".woff2"))
    .filter((path) => {
      const name = path.split(/[\\/]/).pop() ?? "";
      return name.includes("latin-wght") && !name.includes("ext");
    })
    .reduce((sum, path) => sum + gzipSize(path), 0);
  return { initialJs, largestLazyJs, totalJs, totalCss, fonts };
}

/** The figures over budget, each with its measured value and its budget. */
export function check(measured, budget = BUDGET) {
  return Object.keys(budget)
    .filter((name) => measured[name] > budget[name])
    .map((name) => ({ name, measured: measured[name], budget: budget[name] }));
}

function table(measured, budget) {
  const rows = Object.keys(budget).map((name) => {
    const over = measured[name] > budget[name];
    return `${over ? "OVER" : "ok  "}  ${name.padEnd(14)} ${String(measured[name]).padStart(9)} / ${String(budget[name]).padStart(9)}  ${LABELS[name] ?? ""}`;
  });
  return ["      figure            gzip B /  budget B", ...rows].join("\n");
}

function main() {
  const distDir = process.argv[2] ?? "dist";
  const measured = measure(distDir);
  const failures = check(measured);
  console.log(table(measured, BUDGET));
  if (failures.length > 0) {
    console.error(`\nBundle over budget: ${failures.map((failure) => `${failure.name} ${failure.measured} > ${failure.budget}`).join(", ")}`);
    process.exit(1);
  }
  console.log("\nBundle within budget.");
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main();
}
