// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { money, ownerSession, referenceHandlers } from "../planning/testing";

const BROKERAGE = "a0000000-0000-4000-8000-0000000000b1";
const SEC = "5e000000-0000-4000-8000-000000000001";
const STAMP = "2026-11-01T10:00:00Z";

function accounts() {
  const base = referenceHandlers()["GET /api/v1/accounts"];
  return async (call: Parameters<NonNullable<typeof base>>[0]) => {
    const body = (await (base?.(call) as Response).json()) as { items: Record<string, unknown>[] };
    return json({
      items: [
        ...body.items,
        { ...body.items[0], id: BROKERAGE, name: "Brokerage", type: "brokerage", currency: "USD" },
      ],
      next_cursor: null,
    });
  };
}

const gains = (total: string, short: string, long: string) => ({ total: money(total), short_term: money(short), long_term: money(long) });

const holdings = {
  as_of: "2026-11-03",
  base_currency: "USD",
  market_value: money("1250.00"),
  cost_basis: money("1000.00"),
  unrealized: gains("250.00", "50.00", "200.00"),
  accounts: [{ account_id: BROKERAGE, cost_method: "fifo", market_value_base: money("1250.00") }],
  allocation: {
    asset_class: [{ key: "equity", value: money("1250.00"), weight: "1" }],
    sector: [{ key: "technology", value: money("1250.00"), weight: "0.4" }],
    account: [{ key: BROKERAGE, value: money("1250.00"), weight: "1" }],
  },
  unpriced_security_ids: [],
  missing_rates: [],
  holdings: [
    {
      account_id: BROKERAGE,
      security: { id: SEC, symbol: "ACME", name: "Acme Corp", asset_class: "equity", currency: "USD", sector: "technology" },
      quantity: "10.5",
      cost_basis: money("1000.00"),
      price: { basis: "price", close: "119.0476", currency: "USD", price_date: "2026-11-03" },
      market_value: money("1250.00"),
      market_value_base: money("1250.00"),
      unrealized: gains("250.00", "50.00", "200.00"),
      weight: "1",
      lots: [
        {
          id: "lot-1",
          open_date: "2025-01-10",
          quantity: "10.5",
          remaining_quantity: "10.5",
          cost_per_unit: "95.2381",
          cost_basis: money("1000.00"),
          market_value: money("1250.00"),
          unrealized_gain: money("250.00"),
          term: "long",
        },
      ],
    },
  ],
};

describe("investments page", () => {
  it("shows holdings with gains as a percent of cost, allocation and lots", async () => {
    mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () => json(holdings),
    });
    renderApp("/investments");
    const table = await screen.findByRole("table", { name: "Holdings" });
    const row = within(table).getByRole("row", { name: /ACME/ });
    expect(within(row).getByText("10.5")).toBeInTheDocument();
    expect(within(row).getByText("+25.00%", { exact: false })).toBeInTheDocument();
    expect(within(row).getByText("+$250.00")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Allocation by asset class" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Technology: 40.0% of the portfolio" })).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: /Show lots of ACME/ }));
    const lots = await screen.findByRole("table", { name: "Lots of Acme Corp" });
    expect(within(lots).getByText("Long term")).toBeInTheDocument();
  });

  it("records a buy with the cash cost computed from quantity and price", async () => {
    const api = mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/investments/holdings": () => json(holdings),
      "GET /api/v1/investments/transactions": () => json({ items: [], next_cursor: null }),
      "GET /api/v1/securities": () =>
        json({
          items: [{ id: SEC, symbol: "ACME", name: "Acme Corp", asset_class: "equity", currency: "USD", isin: null, cusip: null, sector: null, exchange: null, created_at: STAMP, updated_at: STAMP }],
          next_cursor: null,
        }),
      "POST /api/v1/investments/transactions": () => json({ id: "t-1" }, 201),
    });
    renderApp("/investments");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Transactions" }));
    await user.click(await screen.findByRole("button", { name: "New transaction" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText(/Security/), SEC);
    await user.type(within(dialog).getByLabelText(/Quantity/), "3");
    await user.type(within(dialog).getByLabelText(/Price per unit/), "10.10");
    await user.click(within(dialog).getByRole("button", { name: "Record transaction" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/investments/transactions")).toHaveLength(1); });
    expect(api.callsTo("POST", "/api/v1/investments/transactions")[0]?.body).toMatchObject({
      account_id: BROKERAGE,
      type: "buy",
      quantity: "3",
      price: "10.10",
      amount: { amount: "-30.30", currency: "USD" },
    });
  });
});
