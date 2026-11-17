// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, noContent, problem, renderApp, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, money, ownerSession, referenceHandlers } from "../planning/testing";
import { account, page } from "../transactions/fixtures";

const ASSET = "20000000-0000-4000-8000-000000000001";
const STAMP = "2026-11-01T10:00:00Z";
const valuation = (id: string, on: string, amount: string) => ({ id, account_id: ACCOUNT_CHECKING, valued_on: on, value: money(amount), source: "manual", created_at: STAMP });
const asset = {
  id: ASSET,
  account_id: ACCOUNT_CHECKING,
  name: "Family home",
  kind: "property",
  description: null,
  valuation_method: "appraisal",
  current_value: valuation("v2", "2026-06-01", "330000.00"),
  created_at: STAMP,
  updated_at: STAMP,
};

/** The asset's account; its hub holds the Valuations tab. */
const HOME = account(ACCOUNT_CHECKING, "Family home", { type: "manual_asset", asset_kind: "property", balance: money("330000.00"), balance_date: "2026-06-01" });
const HUB = `/accounts/${ACCOUNT_CHECKING}`;

describe("assets", () => {
  it("shows each asset's kind and how it is valued on its account in the property group", async () => {
    const home: Handler = async (call) => {
      const base = referenceHandlers()["GET /api/v1/accounts"] ?? (() => json({ items: [], next_cursor: null }));
      const body = (await (await base(call)).json()) as { items: Record<string, unknown>[] };
      return json({ items: body.items.map((entry) => ({ ...entry, name: "Family home", type: "manual_asset", balance: money("330000.00"), balance_date: "2026-06-01" })), next_cursor: null });
    };
    mockApi({ ...ownerSession(), ...referenceHandlers(), "GET /api/v1/accounts": home, "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }) });
    renderApp("/assets");
    const group = await screen.findByRole("region", { name: "Property and other" });
    expect(within(within(group).getByRole("list", { name: "Property and other" })).getByText("$330,000.00")).toBeInTheDocument();
    expect(await within(group).findByText("Property · Appraisal")).toBeInTheDocument();
    expect(within(group).getByText("Your estimate, June 2026")).toBeInTheDocument();
    expect(within(group).getByRole("link", { name: "Valuations of Family home" })).toHaveAttribute("href", `/accounts/${ACCOUNT_CHECKING}?tab=valuations`);
  });

  it("adds details to a manual asset account that has none yet", async () => {
    const home: Handler = async (call) => {
      const base = referenceHandlers()["GET /api/v1/accounts"] ?? (() => json({ items: [], next_cursor: null }));
      const body = (await (await base(call)).json()) as { items: Record<string, unknown>[] };
      return json({ items: body.items.map((entry) => ({ ...entry, name: "Family home", type: "manual_asset" })), next_cursor: null });
    };
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": home,
      "GET /api/v1/assets": () => json({ items: [], next_cursor: null }),
      "POST /api/v1/assets": () => json(asset, 201),
    });
    renderApp("/accounts?group=property");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add asset details for Family home" }));
    const dialog = await screen.findByRole("dialog", { name: "Asset details for Family home" });
    await user.selectOptions(within(dialog).getByLabelText("How you value it"), "appraisal");
    await user.type(within(dialog).getByLabelText(/^First value/), "330000");
    await user.click(within(dialog).getByRole("button", { name: "Add asset" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/assets")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/assets")[0]?.body).toMatchObject({
      account_id: ACCOUNT_CHECKING,
      valuation_method: "appraisal",
      value: { amount: "330000", currency: "USD" },
    });
  });

  it("shows the valuation history and adds a value", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00"), valuation("v1", "2025-06-01", "300000.00")] }),
      "POST /api/v1/assets/:id/valuations": () => json(valuation("v3", "2026-11-03", "335000.00"), 201),
    });
    renderApp(`${HUB}?tab=valuations`);
    expect(await screen.findByRole("tab", { name: "Valuations", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "Asset details" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Asset details" })).getByText("Family home")).toBeInTheDocument();
    expect(await screen.findByText("Appraisal")).toBeInTheDocument();
    expect(await screen.findByText(/Since the first value/)).toHaveTextContent("+$30,000.00 (+10.00%)");
    const user = userEvent.setup();
    const form = screen.getByRole("form", { name: "Add a value" });
    await user.type(within(form).getByLabelText(/Value/), "335000");
    await user.click(within(form).getByRole("button", { name: "Save value" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/assets/:id/valuations")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/assets/:id/valuations")[0]?.body).toMatchObject({ value: { amount: "335000", currency: "USD" } });
  });

  it("asks before deleting a value", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00"), valuation("v1", "2025-06-01", "300000.00")] }),
      "DELETE /api/v1/assets/:id/valuations/:vid": () => new Response(null, { status: 204 }),
    });
    renderApp(`${HUB}?tab=valuations`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete the value of 2025-06-01" }));
    let dialog = await screen.findByRole("dialog", { name: "Delete this value?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Delete this value?" })).not.toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/assets/:id/valuations/:vid")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Delete the value of 2025-06-01" }));
    dialog = await screen.findByRole("dialog", { name: "Delete this value?" });
    api.on("GET /api/v1/assets/:id/valuations", () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }));
    await user.click(within(dialog).getByRole("button", { name: "Delete value" }));
    await vi.waitFor(() => { expect(api.callsTo("DELETE", "/api/v1/assets/:id/valuations/:vid")).toHaveLength(1); });
    expect(api.callsTo("DELETE", "/api/v1/assets/:id/valuations/:vid")[0]?.path).toBe(`/api/v1/assets/${ASSET}/valuations/v1`);
    await vi.waitFor(() => { expect(screen.queryByRole("dialog", { name: "Delete this value?" })).not.toBeInTheDocument(); });
    // The row and its button are gone: focus goes to the history table, not the page body.
    await vi.waitFor(() => { expect(screen.queryByRole("button", { name: "Delete the value of 2025-06-01" })).not.toBeInTheDocument(); });
    await vi.waitFor(() => { expect(screen.getByRole("region", { name: "Valuation history" })).toHaveFocus(); });
  });

  it("moves focus to the history only once the confirm is closed", async () => {
    // A modal dialog makes the page inert: focus moved while the confirm is
    // still open is lost. jsdom has no showModal, so stand one in.
    const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
    const saved = { showModal: proto["showModal"], close: proto["close"] };
    proto["showModal"] = function (this: HTMLDialogElement) { this.setAttribute("open", ""); };
    proto["close"] = function (this: HTMLDialogElement) { this.removeAttribute("open"); };
    try {
      const api = mockApi({
        ...ownerSession(),
        ...referenceHandlers(),
        "GET /api/v1/accounts": page([HOME]),
        "GET /api/v1/accounts/:id": () => json(HOME),
        "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
        "GET /api/v1/assets/:id": () => json(asset),
        "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00"), valuation("v1", "2025-06-01", "300000.00")] }),
        "DELETE /api/v1/assets/:id/valuations/:vid": () => new Response(null, { status: 204 }),
      });
      renderApp(`${HUB}?tab=valuations`);
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Delete the value of 2025-06-01" }));
      const dialog = await screen.findByRole("dialog", { name: "Delete this value?" });
      const region = screen.getByRole("region", { name: "Valuation history" });
      const openAtFocus: boolean[] = [];
      region.addEventListener("focus", () => { openAtFocus.push(document.querySelector("dialog[open]") !== null); });
      api.on("GET /api/v1/assets/:id/valuations", () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }));
      await user.click(within(dialog).getByRole("button", { name: "Delete value" }));
      await vi.waitFor(() => { expect(region).toHaveFocus(); });
      expect(openAtFocus).toEqual([false]);
    } finally {
      proto["showModal"] = saved.showModal;
      proto["close"] = saved.close;
    }
  });

  it("offers Add valuation, not Add transaction, as an asset account's main action", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/accounts/:id/balances": () => json({ account_id: ACCOUNT_CHECKING, currency: "USD", start: "2025-11-01", end: "2026-11-03", items: [], snapshots: [] }),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }),
    });
    const { router } = renderApp(HUB);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add valuation" }));
    expect(screen.queryByRole("button", { name: "Add transaction" })).not.toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: "Valuations", selected: true })).toBeInTheDocument();
    expect(currentHref(router)).toBe(`${HUB}?tab=valuations`);
    const form = await screen.findByRole("form", { name: "Add a value" });
    await vi.waitFor(() => { expect(within(form).getByLabelText(/^Value/)).toHaveFocus(); });
    // A transaction can still be added, from the account's menu.
    await user.click(screen.getByRole("button", { name: "Actions for Family home" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for Family home" })).getByRole("menuitem", { name: "Add transaction" }));
    expect(await screen.findByRole("dialog", { name: "Add transaction" })).toBeInTheDocument();
  });

  it("Add valuation focuses the value field once, not again when history returns to Valuations", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/accounts/:id/balances": () => json({ account_id: ACCOUNT_CHECKING, currency: "USD", start: "2025-11-01", end: "2026-11-03", items: [], snapshots: [] }),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }),
    });
    const { router } = renderApp(HUB);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add valuation" }));
    const form = await screen.findByRole("form", { name: "Add a value" });
    await vi.waitFor(() => { expect(within(form).getByLabelText(/^Value/)).toHaveFocus(); });
    // Leave Valuations without the tab control (a pushed entry), then come back with Back.
    router.history.push(HUB);
    expect(await screen.findByRole("tab", { name: "Overview", selected: true })).toBeInTheDocument();
    router.history.back();
    const again = await screen.findByRole("form", { name: "Add a value" });
    expect(screen.getByRole("tab", { name: "Valuations", selected: true })).toBeInTheDocument();
    // Give a stray effect the chance to run before checking.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(within(again).getByLabelText(/^Value/)).not.toHaveFocus();
  });

  it("Add valuation on an asset account without details asks for them with a first value", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/accounts/:id/balances": () => json({ account_id: ACCOUNT_CHECKING, currency: "USD", start: "2025-11-01", end: "2026-11-03", items: [], snapshots: [] }),
      "GET /api/v1/assets": () => json({ items: [], next_cursor: null }),
    });
    renderApp(HUB);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add valuation" }));
    const dialog = await screen.findByRole("dialog", { name: "Asset details for Family home" });
    expect(within(dialog).getByLabelText(/^First value/)).toBeInTheDocument();
  });

  it("deleting the asset details keeps the account", async () => {
    const state = { items: [asset] };
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/accounts/:id/balances": () => json({ account_id: ACCOUNT_CHECKING, currency: "USD", start: "2025-11-01", end: "2026-11-03", items: [], snapshots: [] }),
      "GET /api/v1/assets": () => json({ items: state.items, next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [] }),
      "DELETE /api/v1/assets/:id": () => {
        state.items = [];
        return noContent();
      },
    });
    const { router } = renderApp(`${HUB}?tab=valuations`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete asset" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete asset" });
    expect(dialog).toHaveTextContent("The account and its values stay.");
    await user.click(within(dialog).getByRole("button", { name: "Delete asset" }));
    await vi.waitFor(() => { expect(api.callsTo("DELETE", `/api/v1/assets/${ASSET}`)).toHaveLength(1); });
    await vi.waitFor(() => { expect(currentHref(router)).toBe(HUB); });
    expect(api.callsTo("DELETE", "/api/v1/accounts/:id")).toHaveLength(0);
  });

  it("shows a viewer the values without the controls to change them", async () => {
    mockApi({
      ...referenceHandlers(),
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/accounts": page([{ ...HOME, can_edit: false }]),
      "GET /api/v1/accounts/:id": () => json({ ...HOME, can_edit: false }),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }),
    });
    renderApp(`${HUB}?tab=valuations`);
    const history = await screen.findByRole("table", { name: "Valuation history" });
    expect(await within(history).findByText("Jun 1, 2026")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add a value" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete the value/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete asset" })).not.toBeInTheDocument();
  });

  it("does not say an asset account has no details when the assets could not be loaded", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/assets": () => problem(500, "server-error"),
    });
    renderApp(`${HUB}?tab=valuations`);
    const panel = await screen.findByRole("tabpanel");
    expect(await within(panel).findByText("The asset details could not be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("This account has no asset details yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add asset details" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/accounts": page([HOME]),
      "GET /api/v1/accounts/:id": () => json(HOME),
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00")] }),
    });
    const { container } = renderApp(`${HUB}?tab=valuations`);
    await screen.findByRole("table", { name: "Valuation history" });
    await screen.findByText("Appraisal");
    await expectNoAxeViolations(container);
  });
});
