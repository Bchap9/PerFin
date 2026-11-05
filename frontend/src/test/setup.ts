// SPDX-License-Identifier: AGPL-3.0-or-later
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";

// jsdom does not implement scrolling; the router's scroll restoration calls it.
Object.defineProperty(window, "scrollTo", { value: () => undefined, writable: true });

// No test reaches the network. Unmocked API calls get a 404 (as if the
// endpoint did not exist yet); tests that need responses stub fetch themselves.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(null, { status: 404, statusText: "Not Found" }))),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  try {
    window.localStorage.clear();
  } catch {
    // storage unavailable in this environment
  }
  delete document.documentElement.dataset["theme"];
});
