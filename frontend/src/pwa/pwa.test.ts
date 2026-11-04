// SPDX-License-Identifier: AGPL-3.0-or-later
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { runInNewContext } from "node:vm";

import { registerServiceWorker, scriptUrl, SERVICE_WORKER_URL } from "./register";

const ROOT = join(import.meta.dirname, "..", "..");
const read = (path: string): string => readFileSync(join(ROOT, path), "utf8");

type Listener = (event: unknown) => void;

/** Run public/sw.js against a fake worker scope and an in-memory Cache Storage. */
function loadWorker() {
  const listeners = new Map<string, Listener>();
  const stored = new Map<string, Map<string, Response>>();
  const fetched: string[] = [];
  const cacheFor = (name: string) => {
    const entries = stored.get(name) ?? new Map<string, Response>();
    stored.set(name, entries);
    return {
      match: (request: Request) => Promise.resolve(entries.get(request.url)),
      put: (request: Request, response: Response) => {
        entries.set(request.url, response);
        return Promise.resolve();
      },
    };
  };
  const caches = {
    open: (name: string) => Promise.resolve(cacheFor(name)),
    keys: () => Promise.resolve([...stored.keys()]),
    delete: (name: string) => Promise.resolve(stored.delete(name)),
  };
  let claimed = false;
  let skipped = false;
  const self = {
    location: { origin: "https://perfin.test" },
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
    skipWaiting: () => {
      skipped = true;
      return Promise.resolve();
    },
    clients: {
      claim: () => {
        claimed = true;
        return Promise.resolve();
      },
    },
  };
  const fakeFetch = (request: Request) => {
    fetched.push(request.url);
    // Node's Response says "default"; a same-origin fetch result says "basic".
    const response = new Response("body", { status: 200 });
    Object.defineProperty(response, "type", { value: "basic" });
    return Promise.resolve(response);
  };
  runInNewContext(read("public/sw.js"), { self, caches, fetch: fakeFetch, URL, Promise, Response });

  /** Dispatch a fetch event; returns whether the worker took over (respondWith called) and the result. */
  async function dispatch(url: string, method = "GET") {
    let responded: Promise<Response> | undefined;
    listeners.get("fetch")?.({
      request: { url, method },
      respondWith: (value: Promise<Response>) => {
        responded = value;
      },
    });
    return { handled: responded !== undefined, response: responded === undefined ? undefined : await responded };
  }
  return { listeners, stored, fetched, dispatch, state: () => ({ claimed, skipped }) };
}

describe("service worker", () => {
  it("caches fingerprinted bundles and icons, serving repeats from the cache", async () => {
    const worker = loadWorker();
    const bundle = "https://perfin.test/static/index-abc123.js";
    expect((await worker.dispatch(bundle)).handled).toBe(true);
    expect((await worker.dispatch(bundle)).handled).toBe(true);
    expect(worker.fetched).toEqual([bundle]);
    expect((await worker.dispatch("https://perfin.test/icon-192.png")).handled).toBe(true);
    expect([...(worker.stored.get("perfin-static-v1")?.keys() ?? [])]).toEqual([bundle, "https://perfin.test/icon-192.png"]);
  });

  it.each([
    ["an API read", "https://perfin.test/api/v1/accounts", "GET"],
    ["an API read with a query", "https://perfin.test/api/v1/transactions?limit=5", "GET"],
    ["the session", "https://perfin.test/api/v1/auth/session", "GET"],
    ["the event stream", "https://perfin.test/api/v1/events", "GET"],
    ["a document download", "https://perfin.test/api/v1/documents/abc/content", "GET"],
    ["the HTML shell", "https://perfin.test/", "GET"],
    ["a client route", "https://perfin.test/accounts", "GET"],
    ["a bundle with a query string", "https://perfin.test/static/index-abc123.js?x=1", "GET"],
    ["another origin", "https://cdn.example.test/static/index-abc123.js", "GET"],
    ["a write to a static path", "https://perfin.test/static/index-abc123.js", "POST"],
    ["an API write", "https://perfin.test/api/v1/transactions", "POST"],
  ])("never intercepts %s", async (_name, url, method) => {
    const worker = loadWorker();
    const result = await worker.dispatch(url, method);
    expect(result.handled).toBe(false);
    expect(worker.fetched).toEqual([]);
    expect(worker.stored.size).toBe(0);
  });

  it("does not store failed or non-basic responses", () => {
    const source = read("public/sw.js");
    expect(source).toContain('response.status === 200 && response.type === "basic"');
  });

  it("deletes old caches on activate and takes control", async () => {
    const worker = loadWorker();
    worker.stored.set("perfin-static-v0", new Map());
    worker.stored.set("perfin-static-v1", new Map());
    worker.listeners.get("install")?.({});
    let done: Promise<unknown> | undefined;
    worker.listeners.get("activate")?.({
      waitUntil: (value: Promise<unknown>) => {
        done = value;
      },
    });
    await done;
    expect([...worker.stored.keys()]).toEqual(["perfin-static-v1"]);
    expect(worker.state()).toEqual({ claimed: true, skipped: true });
  });

  it("never mentions /api, so no API response can be cached", () => {
    // The allow-list is the only way in; guard against a future edit adding one.
    const code = read("public/sw.js").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    expect(code).not.toMatch(/\/api/);
    expect(code).not.toMatch(/importScripts|eval\(|new Function/);
  });
});

describe("web app manifest and head", () => {
  const manifest = JSON.parse(read("public/manifest.webmanifest")) as {
    name: string;
    start_url: string;
    scope: string;
    display: string;
    theme_color: string;
    background_color: string;
    icons: { src: string; sizes: string; type: string; purpose: string }[];
  };

  it("is installable: name, standalone display, start URL and 192/512 icons", () => {
    expect(manifest.name).toBe("PerFin");
    expect(manifest.display).toBe("standalone");
    expect(manifest.start_url).toBe("/");
    expect(manifest.scope).toBe("/");
    expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/);
    expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/);
    const sizes = manifest.icons.map((icon) => icon.sizes);
    expect(sizes).toContain("192x192");
    expect(sizes).toContain("512x512");
    expect(manifest.icons.some((icon) => icon.purpose === "maskable")).toBe(true);
  });

  it("points only at files that exist at the site root, as real PNGs of the stated size", () => {
    for (const icon of manifest.icons) {
      expect(icon.src).toMatch(/^\/[a-z0-9-]+\.png$/);
      const path = join(ROOT, "public", icon.src);
      expect(existsSync(path)).toBe(true);
      const bytes = readFileSync(path);
      expect([...bytes.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
      const [width, height] = icon.sizes.split("x").map(Number);
      expect([bytes.readUInt32BE(16), bytes.readUInt32BE(20)]).toEqual([width, height]);
    }
  });

  it("is linked from index.html with theme colours and icons, and no inline script or style", () => {
    const html = read("index.html");
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" />');
    expect(html).toContain('rel="apple-touch-icon"');
    expect(html).toContain('name="theme-color"');
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/);
    expect(html).not.toMatch(/<style|style=|onload=|onclick=/);
  });
});

describe("service worker registration", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("registers /sw.js at the root scope when enabled", () => {
    const register = vi.fn(() => Promise.resolve({}));
    vi.stubGlobal("navigator", { serviceWorker: { register } });
    Object.defineProperty(document, "readyState", { value: "complete", configurable: true });
    registerServiceWorker(true);
    expect(register).toHaveBeenCalledWith(SERVICE_WORKER_URL, { scope: "/" });
  });

  it("does nothing in development or without service worker support", () => {
    const register = vi.fn(() => Promise.resolve({}));
    vi.stubGlobal("navigator", { serviceWorker: { register } });
    registerServiceWorker(false);
    vi.stubGlobal("navigator", {});
    registerServiceWorker(true);
    expect(register).not.toHaveBeenCalled();
  });

  it("goes through a Trusted Types policy that allows only the worker URL", () => {
    const created: string[] = [];
    const factory = {
      createPolicy: (name: string, rules: { createScriptURL: (input: string) => string }) => {
        created.push(name);
        return { createScriptURL: rules.createScriptURL };
      },
    };
    expect(scriptUrl(factory)).toBe("/sw.js");
    expect(created).toEqual(["perfin-service-worker"]);
    const rules: { fn?: (input: string) => string } = {};
    scriptUrl({
      createPolicy: (_name, given) => {
        rules.fn = given.createScriptURL;
        return given;
      },
    });
    expect(() => rules.fn?.("https://evil.test/x.js")).toThrow(TypeError);
    expect(scriptUrl(undefined)).toBe("/sw.js");
  });
});
