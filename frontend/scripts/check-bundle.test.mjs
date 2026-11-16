// SPDX-License-Identifier: AGPL-3.0-or-later
// Run with `node --test scripts/check-bundle.test.mjs` (not part of vitest).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

import { BUDGET, check, measure } from "./check-bundle.mjs";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "check-bundle.mjs");
const gz = (text) => gzipSync(Buffer.from(text), { level: 9 }).length;

function fixture(files) {
  const dir = mkdtempSync(join(tmpdir(), "perfin-bundle-"));
  for (const [name, content] of Object.entries(files)) {
    const path = join(dir, name);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, content);
  }
  return dir;
}

const INDEX = `<!doctype html>
<html><head>
<script type="module" crossorigin src="/static/index-a.js"></script>
<link rel="modulepreload" crossorigin href="/static/vendor-b.js">
<link rel="stylesheet" crossorigin href="/static/index-c.css">
</head><body><div id="root"></div></body></html>`;

test("sums the entry and its preloads", () => {
  const entry = "console.log('entry');".repeat(50);
  const vendor = "export const v = 1;".repeat(80);
  const lazy = "export const lazy = 2;".repeat(300);
  const css = "body{color:red}".repeat(20);
  const dir = fixture({
    "index.html": INDEX,
    "static/index-a.js": entry,
    "static/vendor-b.js": vendor,
    "static/Page-d.js": lazy,
    "static/index-c.css": css,
    "static/figtree-latin-wght-normal-x.woff2": "font-a".repeat(100),
    "static/figtree-latin-ext-wght-normal-y.woff2": "font-b".repeat(100),
  });
  try {
    const measured = measure(dir);
    assert.equal(measured.initialJs, gz(entry) + gz(vendor));
    assert.equal(measured.largestLazyJs, gz(lazy));
    assert.equal(measured.totalJs, gz(entry) + gz(vendor) + gz(lazy));
    assert.equal(measured.totalCss, gz(css));
    assert.equal(measured.fonts, gz("font-a".repeat(100)));
    assert.deepEqual(check(measured, BUDGET), []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("fails over budget with the figures", () => {
  // Random text does not compress, so this lazy chunk is over 160 kB gzip.
  const big = `export const x = "${randomBytes(260_000).toString("base64")}";`;
  const dir = fixture({
    "index.html": INDEX,
    "static/index-a.js": "console.log(1);",
    "static/vendor-b.js": "export const v = 1;",
    "static/Big-e.js": big,
    "static/index-c.css": "body{}",
  });
  try {
    const failures = check(measure(dir), BUDGET);
    assert.deepEqual(
      failures.map((failure) => failure.name),
      ["largestLazyJs"],
    );
    const run = spawnSync(process.execPath, [SCRIPT, dir], { encoding: "utf8" });
    assert.equal(run.status, 1);
    const output = `${run.stdout}${run.stderr}`;
    assert.match(output, /largestLazyJs/);
    assert.ok(output.includes(String(gz(big))), output);
    assert.ok(output.includes(String(BUDGET.largestLazyJs)), output);
    assert.match(output, /over budget/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("passes a bundle within budget", () => {
  const dir = fixture({
    "index.html": INDEX,
    "static/index-a.js": "console.log(1);",
    "static/vendor-b.js": "export const v = 1;",
    "static/index-c.css": "body{}",
  });
  try {
    const run = spawnSync(process.execPath, [SCRIPT, dir], { encoding: "utf8" });
    assert.equal(run.status, 0, run.stdout + run.stderr);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
