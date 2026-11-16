// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, money, ownerSession, referenceHandlers } from "../planning/testing";

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
      "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }),
      "GET /api/v1/assets/:id": () => json(asset),
      "GET /api/v1/assets/:id/valuations": () => json({ items: [valuation("v2", "2026-06-01", "330000.00"), valuation("v1", "2025-06-01", "300000.00")] }),
      "POST /api/v1/assets/:id/valuations": () => json(valuation("v3", "2026-11-03", "335000.00"), 201),
    });
    renderApp(`/assets/${ASSET}`);
    expect(await screen.findByText(/Since the first value/)).toHaveTextContent("+$30,000.00 (+10.00%)");
    const user = userEvent.setup();
    const form = screen.getByRole("form", { name: "Add a value" });
    await user.type(within(form).getByLabelText(/Value/), "335000");
    await user.click(within(form).getByRole("button", { name: "Save value" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/assets/:id/valuations")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/assets/:id/valuations")[0]?.body).toMatchObject({ value: { amount: "335000", currency: "USD" } });
  });
});
