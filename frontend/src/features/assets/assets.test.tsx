// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
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
  it("lists assets with their current value", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers(), "GET /api/v1/assets": () => json({ items: [asset], next_cursor: null }) });
    renderApp("/assets");
    const row = within(await screen.findByRole("table", { name: "Manual assets" })).getByRole("row", { name: /Family home/ });
    expect(within(row).getByText("$330,000.00")).toBeInTheDocument();
    expect(within(row).getByText("Appraisal")).toBeInTheDocument();
  });

  it("shows the valuation history and adds a value", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
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
