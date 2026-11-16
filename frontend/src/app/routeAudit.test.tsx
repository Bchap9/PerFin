// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The route audit (plan, Task 22): every page of the new route map, rendered
 * at its new address against a fully mocked API, has a heading, exactly one
 * `h1`, a `main` named after that `h1`, and no axe violations in either
 * theme. Then the three keyboard journeys of spec section 8, the phone
 * layout and the 24 px target size.
 *
 * The API mock answers every GET the backend declares (`backend/openapi.json`)
 * with a small body built from the response schema: one row in every list,
 * ids that point at each other (`a-1`, `d-1`, `s-1`, ...), plausible dates and
 * amounts. A few bodies are fixed by hand where a page needs a particular
 * state. Any call the backend does not declare fails the test, so a page
 * cannot pass the audit while showing "could not be loaded".
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  ANON_SESSION,
  authedSession,
  json,
  mockApi,
  pendingSession,
  renderApp,
  type Handler,
  type SessionOptions,
} from "../features/auth/testing";
import { tx } from "../features/transactions/fixtures";
import { expectNoAxeViolations } from "../test/axe";
import { NEW_ROUTES, SECTIONS, SETTINGS_GROUPS } from "./routeMap";
import { THEME_STORAGE_KEY } from "./theme";

const ROOT = join(__dirname, "..", "..", "..");
const NOW = "2026-11-03T10:00:00Z";
const TODAY = "2026-11-03";

// ---------------------------------------------------------------------------
// The shared fixture: a response body for every GET the backend declares.

type Schema = Readonly<Record<string, unknown>>;

const OPENAPI = JSON.parse(readFileSync(join(ROOT, "backend", "openapi.json"), "utf8")) as {
  paths: Record<string, Record<string, Schema>>;
  components: { schemas: Record<string, Schema> };
};
const SCHEMAS = new Map(Object.entries(OPENAPI.components.schemas));

/** The fixture id of each object kind, so references between bodies resolve. */
const IDS = new Map([
  ["AccountOut", "a-1"],
  ["DocumentListItem", "d-1"],
  ["DocumentDetailOut", "d-1"],
  ["DocumentOut", "d-1"],
  ["BudgetOut", "b-1"],
  ["SubscriptionOut", "s-1"],
  ["SubscriptionDetail", "s-1"],
  ["RuleOut", "r-1"],
  ["TransactionOut", "t-1"],
  ["CategoryOut", "c-1"],
  ["PayeeOut", "p-1"],
  ["TagOut", "g-1"],
  ["HouseholdOut", "h-1"],
  ["GoalOut", "goal-1"],
]);

/** Reference fields and the fixture id they point at. */
const REFS = new Map([
  ["account_id", "a-1"],
  ["document_id", "d-1"],
  ["source_document_id", "d-1"],
  ["budget_id", "b-1"],
  ["subscription_id", "s-1"],
  ["schedule_id", "s-1"],
  ["rule_id", "r-1"],
  ["category_rule_id", "r-1"],
  ["transaction_id", "t-1"],
  ["category_id", "c-1"],
  ["suggested_category_id", "c-1"],
  ["payee_id", "p-1"],
  ["household_id", "h-1"],
  ["user_id", "u-1"],
  ["goal_id", "goal-1"],
]);

const DECIMAL = /(^|_)(amount|rate|price|quantity|value|balance|total|percent|share|ratio|units|cost|gain|return|apr|principal|limit|spent|remaining|budgeted|income|expense|net|delta|change|weight|fraction|confidence|score|payment|interest|escrow|target|contributed|saved|progress)s?($|_)/;

function ref(schema: Schema): Schema {
  const target = schema["$ref"];
  if (typeof target !== "string") return schema;
  const name = target.split("/").at(-1) ?? "";
  return SCHEMAS.get(name) ?? {};
}

function refName(schema: Schema): string | undefined {
  const target = schema["$ref"];
  return typeof target === "string" ? target.split("/").at(-1) : undefined;
}

function stringFor(schema: Schema, field: string, owner: string | undefined): string {
  const format = schema["format"];
  if (field === "id" && owner !== undefined && IDS.has(owner)) return IDS.get(owner) ?? "x-1";
  const reference = REFS.get(field);
  if (reference !== undefined) return reference;
  if (format === "date") return TODAY;
  if (format === "date-time") return NOW;
  if (format === "uuid") return field === "id" ? "x-1" : "x-1";
  if (field.endsWith("currency")) return "USD";
  if (field === "email" || field.endsWith("_email")) return "person@example.test";
  if (field.endsWith("_url") || field === "url") return "https://example.test/";
  if (field === "country" || field.endsWith("_country")) return "US";
  if (DECIMAL.test(field)) return "12.34";
  if (field === "name" || field === "display_name" || field.endsWith("_name") || field === "title" || field === "label") {
    return `Example ${field.replaceAll("_", " ")}`;
  }
  return "example";
}

function numberFor(field: string): number {
  if (field === "year" || field.endsWith("_year")) return 2025;
  if (field === "month") return 11;
  if (field.endsWith("_day") || field === "day") return 15;
  return 1;
}

/** A small, valid body for `schema`; `field` is the property it fills, `owner` the object kind. */
function example(input: Schema, field = "", owner?: string, depth = 0): unknown {
  // One page of results: a cursor would make a paging list fetch the same row again and again.
  if (field === "next_cursor") return null;
  const name = refName(input);
  const schema = ref(input);
  const kind = name ?? owner;
  if ("const" in schema) return schema["const"];
  const enumValues = schema["enum"];
  if (Array.isArray(enumValues)) return enumValues[0];
  const anyOf = schema["anyOf"] ?? schema["oneOf"];
  if (Array.isArray(anyOf)) {
    const options = anyOf as Schema[];
    const nonNull = options.find((option) => option["type"] !== "null");
    const nullable = options.some((option) => option["type"] === "null");
    // A nullable field is left out (null) below a few levels, so bodies stay small.
    if (nonNull === undefined || (depth > 3 && nullable)) return null;
    return example(nonNull, field, kind, depth);
  }
  const allOf = schema["allOf"];
  if (Array.isArray(allOf)) return example((allOf as Schema[])[0] ?? {}, field, kind, depth);
  switch (schema["type"]) {
    case "string":
      return stringFor(schema, field, kind);
    case "integer":
    case "number":
      return numberFor(field);
    case "boolean":
      return field.startsWith("can_") || field === "enabled" || field === "verified";
    case "null":
      return null;
    case "array": {
      const items = (schema["items"] ?? {}) as Schema;
      return depth > 4 ? [] : [example(items, field, undefined, depth + 1)];
    }
    case "object":
    default: {
      const properties = new Map(Object.entries((schema["properties"] ?? {}) as Record<string, Schema>));
      if (properties.size === 0) return {};
      const body = new Map<string, unknown>();
      for (const [key, value] of properties) {
        body.set(key, example(value, key, kind, depth + 1));
      }
      return Object.fromEntries(body);
    }
  }
}

/** The schema of `GET path`'s 200 answer. */
function responseSchema(operation: Schema): Schema | undefined {
  const responses = new Map(Object.entries((operation["responses"] ?? {}) as Record<string, Schema>));
  const ok = responses.get("200");
  if (ok === undefined) return undefined;
  const content = new Map(Object.entries((ok["content"] ?? {}) as Record<string, Schema>));
  return content.get("application/json")?.["schema"] as Schema | undefined;
}

/** The generated body of `GET path` (an OpenAPI path, `{param}` form). */
function declaredBody(path: string): unknown {
  const operation = new Map(Object.entries(new Map(Object.entries(OPENAPI.paths)).get(path) ?? {})).get("get");
  const schema = operation === undefined ? undefined : responseSchema(operation);
  return schema === undefined ? {} : example(schema);
}

/** A handler for every GET the backend declares, keyed for `mockApi`. */
function declaredHandlers(): [string, Handler][] {
  const entries: [string, Handler][] = [];
  for (const [path, operations] of Object.entries(OPENAPI.paths)) {
    if (!new Map(Object.entries(operations)).has("get")) continue;
    const body = declaredBody(path);
    entries.push([`GET ${path.replaceAll(/\{([^}]+)\}/g, ":$1")}`, () => json(body)]);
  }
  // Static segments first: `/accounts/subtypes` wins over `/accounts/:account_id`.
  return entries.sort((a, b) => a[0].split(":").length - b[0].split(":").length);
}

/** The first item of a generated `{ items: [...] }` or `[...]` body. */
function firstItem(body: unknown): Record<string, unknown> {
  const items = Array.isArray(body) ? body : ((body as { items?: unknown[] }).items ?? []);
  return (items.at(0) ?? {}) as Record<string, unknown>;
}

/**
 * Bodies fixed by hand where the generated one (the first value of every
 * enum, `false` for most flags) would show a page in a state with nothing
 * in it: the history needs an answered item, Account access a member other
 * than the owner, sign-in its password form.
 */
const OVERRIDES: Record<string, Handler> = {
  "GET /api/v1/auth/login/options": () => json({ local_login: true, password_reset: true }),
  "GET /api/v1/review": () =>
    json({ items: [{ ...firstItem(declaredBody("/api/v1/review")), state: "resolved" }], next_cursor: null }),
  "GET /api/v1/households/:household_id/members": () => {
    const owner = firstItem(declaredBody("/api/v1/households/{household_id}/members"));
    return json([
      { ...owner, role: "owner", is_active: true },
      { ...owner, user_id: "u-2", username: "example-member", display_name: "Example member", role: "member", is_active: true },
    ]);
  },
};

/** Per-page bodies: the setup page is shown only while setup is still to do. */
const ROUTE_OVERRIDES = new Map<string, Record<string, Handler>>([
  ["/setup", { "GET /api/v1/setup/status": () => json({ setup_required: true }) }],
]);

function auditApi(session: Handler, route = "") {
  // Declared first: a later key replaces the body but keeps the key's place, so static paths still win.
  return mockApi({
    ...Object.fromEntries(declaredHandlers()),
    ...OVERRIDES,
    ...ROUTE_OVERRIDES.get(route),
    "GET /api/v1/auth/session": session,
  });
}

// ---------------------------------------------------------------------------
// The routes.

const PARAMS = new Map([
  ["$documentId", "d-1"],
  ["$budgetId", "b-1"],
  ["$id", "s-1"],
  ["$year", "2025"],
  ["$ruleId", "r-1"],
  ["$accountId", "a-1"],
]);

function fill(path: string): string {
  return path
    .split("/")
    .map((part) => PARAMS.get(part) ?? part)
    .join("/");
}

const OWNER: SessionOptions = { role: "owner", instanceAdmin: true };

/** The session each bare page is shown to; every other page gets the owner. */
const SESSIONS = new Map<string, Handler>([
  ["/login", ANON_SESSION],
  ["/login/mfa", pendingSession()],
  ["/login/enroll-mfa", authedSession({ ...OWNER, enrollmentRequired: true, enrolled: false })],
  ["/login/link-oidc", ANON_SESSION],
  ["/forgot-password", ANON_SESSION],
  ["/reset-password", ANON_SESSION],
  ["/setup", ANON_SESSION],
]);

/** The address each page is opened at (a reset link carries its token in the hash). */
const ADDRESSES = new Map([
  ["/reset-password", "/reset-password#token=reset_EXAMPLE"],
  ["/setup", "/setup#t=setup_EXAMPLE"],
]);

async function openRoute(route: string) {
  const api = auditApi(SESSIONS.get(route) ?? authedSession(OWNER), route);
  const view = renderApp(ADDRESSES.get(route) ?? fill(route));
  const h1 = await screen.findAllByRole("heading", { level: 1 }, { timeout: 5000 });
  // Let the page's queries settle, so the audit sees the page with its data.
  await waitFor(() => {
    expect(view.queryClient.isFetching()).toBe(0);
    expect(document.querySelector("[aria-busy='true'], .spinner")).toBeNull();
  });
  return { ...view, api, h1 };
}

/** Every call that found no handler, as "METHOD /path". */
async function unanswered(api: ReturnType<typeof auditApi>): Promise<string[]> {
  const responses = await Promise.all(api.fetchMock.mock.results.map((result) => result.value as Promise<Response>));
  return responses.flatMap((response, index) => {
    const call = api.calls.at(index);
    return response.status === 404 && call !== undefined ? [`${call.method} ${call.path}`] : [];
  });
}

// What index.html gives every page: the language and the document title.
beforeEach(() => {
  document.documentElement.lang = "en";
  document.title = "PerFin";
});

describe.each(NEW_ROUTES.map((route) => [route]))("%s", (route) => {
  it("renders a heading", async () => {
    const { router, api } = await openRoute(route);
    expect(router.state.location.pathname).toBe(fill(route));
    expect(await unanswered(api)).toEqual([]);
    expect(screen.queryByRole("heading", { name: "Something went wrong" })).not.toBeInTheDocument();
    // The page shows its row, not an error or an empty state.
    // (An empty alert is a live region waiting for a message.)
    expect(
      screen
        .queryAllByRole("alert")
        .map((alert) => alert.textContent)
        .filter((text) => text !== ""),
    ).toEqual([]);
    expect(document.querySelector(".empty-state")?.textContent ?? null).toBeNull();
  });

  it("has exactly one h1", async () => {
    await openRoute(route);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it.each(["light", "dark"] as const)("has no axe violations with data-theme %s", async (theme) => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    await openRoute(route);
    expect(document.documentElement.dataset["theme"]).toBe(theme);
    await expectNoAxeViolations(document.documentElement);
  });

  it("main is labelled by its h1 or the section h1", async () => {
    const { h1 } = await openRoute(route);
    const title = h1.at(0)?.textContent ?? "";
    expect(title).not.toBe("");
    const main = screen.getByRole("main");
    expect(main).toHaveAccessibleName(title);
    expect(document.getElementById(main.getAttribute("aria-labelledby") ?? "")).toBe(h1.at(0));
  });
});

// A Settings link says what the page it opens is called: the side list's
// label is the page's title, word for word.
describe.each(SETTINGS_GROUPS.flatMap((group) => group.pages.map((page) => [page.label, page.to])))(
  "Settings: %s",
  (label, to) => {
    it("opens a page titled with the side list's label", async () => {
      await openRoute(to);
      // The side list has headings of its own; the page sits beside it.
      const page = document.querySelector<HTMLElement>(".settings-shell__page");
      expect(page).not.toBeNull();
      const titles = within(page ?? document.body)
        .getAllByRole("heading", { level: 2 })
        .map((heading) => heading.textContent);
      expect(titles.at(0)).toBe(label);
    });

    it("has one h2, its title, and every other heading under it", async () => {
      await openRoute(to);
      const page = document.querySelector<HTMLElement>(".settings-shell__page");
      const headings = within(page ?? document.body).getAllByRole("heading");
      const levels = headings.map((heading) => `h${heading.tagName.slice(1)} ${heading.textContent ?? ""}`);
      expect(levels.filter((line) => /^h[12] /.test(line))).toEqual([`h2 ${label}`]);
      // A heading inside a card sits under the card's title.
      const nested = headings.flatMap((heading) => {
        const card = heading.closest<HTMLElement>("section.card");
        const title = card?.querySelector(".card__title");
        if (card == null || title == null || title === heading) return [];
        return Number(heading.tagName.slice(1)) > Number(title.tagName.slice(1))
          ? []
          : [`${heading.tagName} ${heading.textContent ?? ""} in ${title.tagName} ${title.textContent ?? ""}`];
      });
      expect(nested).toEqual([]);
    });
  },
);

/** The first item of a generated list body replaced by `patch`, keeping the body's shape. */
function patchFirst(body: unknown, patch: Record<string, unknown>): unknown {
  if (Array.isArray(body)) {
    const rows: unknown[] = body;
    return rows.map((row, index) => (index === 0 ? { ...(row as object), ...patch } : row));
  }
  const items = (body as { items?: unknown[] }).items;
  return items === undefined ? { ...(body as object), ...patch } : { ...(body as object), items: patchFirst(items, patch) };
}

/** The account fixture as another type, in the list and on its own. */
function accountAs(type: string): Record<string, Handler> {
  return {
    "GET /api/v1/accounts": () => json(patchFirst(declaredBody("/api/v1/accounts"), { type })),
    "GET /api/v1/accounts/:account_id": () => json(patchFirst(declaredBody("/api/v1/accounts/{account_id}"), { type })),
  };
}

/**
 * Page states the generated fixture never reaches, because it takes the first
 * value of every enum (a checking account) and each page's default tab: the
 * account hub's loan, asset and investment tabs, and the other Reports views.
 */
const VARIANTS: [address: string, tab: string, overrides: Record<string, Handler>][] = [
  ["/accounts/a-1?tab=schedule", "Schedule", accountAs("mortgage")],
  ["/accounts/a-1?tab=valuations", "Valuations", accountAs("manual_asset")],
  ["/accounts/a-1?tab=holdings", "Holdings", accountAs("brokerage")],
  ["/reports?tab=income", "Income", {}],
  ["/reports?tab=cashflow", "Cash flow", {}],
];

describe.each(VARIANTS)("%s", (address, tab, overrides) => {
  async function openVariant() {
    const api = mockApi({
      ...Object.fromEntries(declaredHandlers()),
      ...OVERRIDES,
      ...overrides,
      "GET /api/v1/auth/session": authedSession(OWNER),
    });
    const view = renderApp(address);
    await screen.findAllByRole("heading", { level: 1 }, { timeout: 5000 });
    await waitFor(() => {
      expect(view.queryClient.isFetching()).toBe(0);
      expect(document.querySelector("[aria-busy='true'], .spinner")).toBeNull();
    });
    return { ...view, api };
  }

  it("opens on its tab, with exactly one h1", async () => {
    const { api } = await openVariant();
    // The tab shows its row, not an error or a "missing" panel.
    expect(await unanswered(api)).toEqual([]);
    expect(
      screen
        .queryAllByRole("alert")
        .map((alert) => alert.textContent)
        .filter((text) => text !== ""),
    ).toEqual([]);
    expect(document.querySelector(".empty-state")?.textContent ?? null).toBeNull();
    // The hub's tabs are a tab list; the Reports views are the section's tab bar of links.
    const chosen =
      screen.queryByRole("tab", { name: tab, selected: true }) ??
      screen.queryAllByRole("link", { name: tab }).find((link) => link.getAttribute("aria-current") === "page");
    expect(chosen).toBeDefined();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Something went wrong" })).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    await openVariant();
    await expectNoAxeViolations(document.documentElement);
  });
});

// ---------------------------------------------------------------------------
// Keyboard journeys (spec section 8). Hidden elements stay tabbable in jsdom
// and the skip link comes first, so each journey tabs until the target has
// focus, within a bound, rather than counting presses.

async function tabUntil(user: ReturnType<typeof userEvent.setup>, target: () => Element | null, limit = 80): Promise<void> {
  for (let presses = 0; presses < limit; presses += 1) {
    const element = target();
    if (element !== null && document.activeElement === element) return;
    await user.tab();
  }
  const focused = document.activeElement;
  throw new Error(
    `focus did not reach the target in ${String(limit)} presses; it is on ${focused?.tagName ?? "nothing"} "${(focused?.textContent ?? "").slice(0, 60)}"`,
  );
}

describe("keyboard journeys", () => {
  it("from Home, Tab reaches the rail, Enter on Inbox, then Tab reaches the first card's action", async () => {
    const api = auditApi(authedSession(OWNER));
    const { router } = renderApp("/");
    await screen.findByRole("heading", { level: 1 });
    const user = userEvent.setup();
    const rail = screen.getByRole("navigation", { name: "Sections" });
    const inbox = within(rail).getByRole("link", { name: /^Inbox/ });
    await tabUntil(user, () => inbox);
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/inbox");
    });
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    const firstCard = within(list).getAllByRole("listitem").at(0);
    const action = firstCard === undefined ? null : within(firstCard).getAllByRole("button").at(0) ?? null;
    expect(action).not.toBeNull();
    await tabUntil(user, () => action);
    expect(action).toHaveFocus();
    expect(await unanswered(api)).toEqual([]);
  });

  it("on Transactions: Tab to the search, type, Tab to the first row, Down, Enter opens the drawer, Escape returns to the row", async () => {
    const api = auditApi(authedSession(OWNER));
    const rows = [tx("t-1", { payee_id: null, raw_payee: "FIRST EXAMPLE SHOP" }), tx("t-2", { payee_id: null, raw_payee: "SECOND EXAMPLE SHOP" })];
    const byId = new Map(rows.map((row) => [row.id, row]));
    api.on("GET /api/v1/transactions", () => json({ items: rows, next_cursor: null }));
    api.on("GET /api/v1/transactions/:id", (call) => json(byId.get(call.path.split("/").at(-1) ?? "") ?? rows[0]));
    renderApp("/transactions");
    const user = userEvent.setup();
    const search = await screen.findByRole("searchbox", { name: "Search transactions" });
    await tabUntil(user, () => search);
    await user.keyboard("shop");
    expect(search).toHaveValue("shop");
    const openButtons = () => screen.queryAllByRole("button", { name: /^Open .*EXAMPLE SHOP/i });
    await waitFor(() => {
      expect(openButtons()).toHaveLength(2);
    });
    await tabUntil(user, () => openButtons().at(0) ?? null);
    await user.keyboard("{ArrowDown}");
    const second = openButtons().at(1);
    expect(second).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(openButtons().at(1)).toHaveFocus();
    });
  });

  it('Ctrl+K, "budg", Enter lands on /plan/budgets', async () => {
    auditApi(authedSession(OWNER));
    const { router } = renderApp("/");
    await screen.findByRole("heading", { level: 1 });
    const user = userEvent.setup();
    await user.keyboard("{Control>}k{/Control}");
    const combobox = await screen.findByRole("combobox", { name: "Go to" });
    expect(combobox).toHaveFocus();
    await user.keyboard("budg");
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/plan/budgets");
    });
    expect(await screen.findByRole("heading", { level: 2, name: "Budgets" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Phone width and target size. jsdom does no layout, so what CSS decides is
// read from the stylesheets themselves.

const STYLES = join(ROOT, "frontend", "src");

/** The `@media` blocks of `css`: each one's query and the text between its braces, from `start` to `end`. */
function mediaBlocks(css: string): { query: string; body: string; start: number; end: number }[] {
  const blocks: { query: string; body: string; start: number; end: number }[] = [];
  let from = css.indexOf("@media");
  while (from !== -1) {
    const open = css.indexOf("{", from);
    let depth = 0;
    let close = open;
    for (let index = open; index < css.length; index += 1) {
      const char = css.charAt(index);
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) {
        close = index;
        break;
      }
    }
    blocks.push({ query: css.slice(from + "@media".length, open).trim(), body: css.slice(open + 1, close), start: from, end: close + 1 });
    from = css.indexOf("@media", close);
  }
  return blocks;
}

/** The declarations of `selector` (exact match in a selector list), at the top level or inside `@media media`. */
function declarations(file: string, selector: string, media?: string): Map<string, string> | undefined {
  const css = readFileSync(join(STYLES, file), "utf8").replaceAll(/\/\*[\s\S]*?\*\//g, "");
  const blocks = mediaBlocks(css);
  const scope =
    media === undefined
      ? blocks.reduceRight((text, block) => text.slice(0, block.start) + text.slice(block.end), css)
      : blocks.find((block) => block.query === media)?.body;
  if (scope === undefined) return undefined;
  for (const rule of scope.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = (rule[1] ?? "").split(",").map((part) => part.trim());
    if (!selectors.includes(selector)) continue;
    return new Map(
      (rule[2] ?? "")
        .split(";")
        .map((line) => line.split(":").map((part) => part.trim()))
        .filter((pair): pair is [string, string] => pair.length >= 2 && pair[0] !== "")
        .map(([property, value]) => [property, value]),
    );
  }
  return undefined;
}

function pixels(value: string | undefined): number {
  const match = /^([\d.]+)(rem|px)$/.exec(value ?? "");
  if (match === null) throw new Error(`not a length: ${String(value)}`);
  return Number(match[1]) * (match[2] === "rem" ? 16 : 1);
}

const PHONE = "(max-width: 819px)";

function stubPhoneWidth(): void {
  const width = 390;
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => {
      const max = /max-width:\s*(\d+)px/.exec(query);
      const min = /min-width:\s*(\d+)px/.exec(query);
      const matches = max !== null ? width <= Number(max[1]) : min !== null ? width >= Number(min[1]) : false;
      return {
        matches,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      };
    }),
  );
}

// The shell's phone layout is CSS only: RootLayout reads no media query, so the
// matchMedia stub below changes nothing it renders (only the theme and the
// subscription pages read matchMedia). The phone-specific checks here are the
// rules read from layout.css under (max-width: 819px); the DOM assertions hold
// at any width. Real widths, and horizontal scroll at 360 px, are checked by
// hand in a browser (plan Task 25); jsdom does no layout.
describe("phone width", () => {
  it("the bottom bar holds the six sections, and the account menu holds Settings", async () => {
    stubPhoneWidth();
    expect(window.matchMedia(PHONE).matches).toBe(true);
    auditApi(authedSession(OWNER));
    const { router } = renderApp("/");
    await screen.findByRole("heading", { level: 1 });

    // The sections nav is the bottom bar under 820 px.
    const bar = declarations("app/styles/layout.css", ".app-nav", PHONE);
    expect(bar?.get("position")).toBe("fixed");
    expect(bar?.get("inset")).toBe("auto 0 0");
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect(nav).toHaveClass("app-nav");
    const sections = within(nav)
      .getAllByRole("link")
      .filter((link) => link.closest(".app-nav__item--settings") === null);
    expect(sections.map((link) => link.querySelector(".app-nav__label")?.textContent)).toEqual(SECTIONS.map((section) => section.label));
    // Settings leaves the bar on a phone ...
    expect(within(nav).getByRole("link", { name: "Settings" }).closest("li")).toHaveClass("app-nav__item--settings");
    expect(declarations("app/styles/layout.css", ".app-nav__item--settings", PHONE)?.get("display")).toBe("none");

    // ... and is in the account menu, in the top bar.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Account: Alice" }));
    const menu = screen.getByRole("menu", { name: "Account: Alice" });
    await user.click(within(menu).getByRole("menuitem", { name: "Settings" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings");
    });
  });
});

describe("target size (WCAG 2.5.8)", () => {
  // Every chip, rail link and icon button is at least 24 by 24 CSS pixels.
  it.each([
    ["components/components.css", ".chip", "min-height", undefined],
    ["app/styles/layout.css", ".app-nav__link", "min-height", undefined],
    ["app/styles/layout.css", ".app-nav__link", "min-height", PHONE],
    ["components/components.css", ".btn", "min-height", undefined],
    ["components/components.css", ".btn--sm", "min-height", undefined],
    ["components/components.css", ".btn--icon", "width", undefined],
    ["components/components.css", ".btn--icon.btn--sm", "width", undefined],
  ] as const)("%s %s has %s of at least 24px %s", (file, selector, property, media) => {
    const rule = declarations(file, selector, media);
    expect(rule, `no rule for ${selector}`).toBeDefined();
    expect(pixels(rule?.get(property))).toBeGreaterThanOrEqual(24);
  });
});
