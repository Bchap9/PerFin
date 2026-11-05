// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ACCOUNT_CHECKING, money, ownerSession, referenceHandlers } from "../planning/testing";
import { json, mockApi, renderApp } from "../auth/testing";

const SCHEDULE = {
  id: "s-1",
  account_id: ACCOUNT_CHECKING,
  name: "Streaming",
  cadence: "monthly",
  interval_count: 1,
  next_due: "2026-11-10",
  amount_min: money("-15.99"),
  amount_max: money("-15.99"),
  payee_id: null,
  category_id: null,
  origin: "detected",
  active: true,
  last_seen_on: "2026-10-10",
};

describe("calendar", () => {
  it("shows what is due with the expected balance", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () =>
        json({
          currency: "USD",
          date_from: "2026-11-03",
          date_to: "2026-12-03",
          opening_balance: money("1200.50"),
          income: money("0"),
          bills: money("-15.99"),
          net: money("-15.99"),
          unconverted: [],
          days: [
            { on: "2026-11-04", events: [], income: money("0"), bills: money("0"), net: money("0"), balance: money("1200.50") },
            {
              on: "2026-11-10",
              events: [{ schedule_id: "s-1", name: "Streaming", account_id: ACCOUNT_CHECKING, kind: "bill", amount: money("-15.99"), amount_min: money("-15.99"), amount_max: money("-15.99"), amount_base: null }],
              income: money("0"),
              bills: money("-15.99"),
              net: money("-15.99"),
              balance: money("1184.51"),
            },
          ],
        }),
    });
    renderApp("/calendar");
    const table = await screen.findByRole("table", { name: "Upcoming days" });
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getByText("Streaming")).toBeInTheDocument();
    expect(within(table).getByText("$1,184.51")).toBeInTheDocument();
  });

  it("lists recurring items and pauses one", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/cashflow/calendar": () => new Response(null, { status: 404 }),
      "GET /api/v1/recurring": () => json({ items: [SCHEDULE] }),
      "GET /api/v1/recurring/candidates": () => json({ as_of: "2026-11-03", items: [] }),
      "PATCH /api/v1/recurring/:id": () => json({ ...SCHEDULE, active: false }),
    });
    renderApp("/calendar");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Recurring and subscriptions" }));
    const row = within(await screen.findByRole("table", { name: "Recurring items" })).getByRole("row", { name: /Streaming/ });
    expect(within(row).getByText("Found for you")).toBeInTheDocument();
    expect(within(row).getByText("Every month")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Pause Streaming" }));
    await vi.waitFor(() => { expect(api.callsTo("PATCH", "/api/v1/recurring/:id")).toHaveLength(1); });
    expect(api.callsTo("PATCH", "/api/v1/recurring/:id")[0]?.body).toEqual({ active: false });
  });
});
