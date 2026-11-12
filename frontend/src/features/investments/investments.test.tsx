// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, noContent, problem, renderApp, type Handler } from "../auth/testing";
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
  accounts: [{ account_id: BROKERAGE, cost_method: "fifo", market_value_base: money("1250.00"), positions_as_of: null as string | null }],
  price_feed: { enabled: false, refresh_minutes: null as number | null },
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
      average_cost: "95.2381",
      price: { basis: "price", close: "119.0476", currency: "USD", price_date: "2026-11-03", source: "manual", updated_at: STAMP },
      price_change: null as Record<string, unknown> | null,
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

  it("says what the figures are as of, where each price came from and how it moved", async () => {
    const [held] = holdings.holdings;
    const fed = {
      ...held,
      price: { basis: "price", close: "119.0476", currency: "USD", price_date: "2026-11-12", source: "http_json", updated_at: "2026-11-12T15:30:00Z" },
      price_change: { since: "2026-11-11", previous_close: "116.6667", value: money("25.00"), fraction: "0.0204080000" },
    };
    mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () =>
        json({
          ...holdings,
          holdings: [fed],
          accounts: [{ ...holdings.accounts[0], positions_as_of: "2026-11-11" }],
          price_feed: { enabled: true, refresh_minutes: 15 },
        }),
    });
    renderApp("/investments");
    const table = await screen.findByRole("table", { name: "Holdings" });
    const row = within(table).getByRole("row", { name: /ACME/ });
    // The account's holdings are those of its last import; the price is newer.
    expect(within(row).getByText("as of Nov 11, 2026")).toBeInTheDocument();
    expect(within(row).getByText("Nov 12, 2026")).toBeInTheDocument();
    expect(within(row).getByText("price feed")).toBeInTheDocument();
    expect(within(row).getByText("+$25.00")).toBeInTheDocument();
    expect(within(row).getByText("(+2.04%)")).toBeInTheDocument();
    expect(within(row).getByText("since Nov 11, 2026")).toBeInTheDocument();
    expect(within(row).getByText("$95.2381")).toBeInTheDocument();
    const note = screen.getByText(/What you hold is as of your last import \(Nov 11, 2026\)/);
    expect(note).toHaveTextContent("Prices are fetched every 15 minutes.");
    expect(note).toHaveTextContent("Market values and gains are estimates");
  });

  it("asks the worker for fresh prices when a feed is set up", async () => {
    const api = mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () => json({ ...holdings, price_feed: { enabled: true, refresh_minutes: 360 } }),
      "POST /api/v1/investments/prices/refresh": () => json({ queued: true }, 202),
    });
    renderApp("/investments");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Refresh prices" }));
    await vi.waitFor(() => { expect(api.callsTo("POST", "/api/v1/investments/prices/refresh")).toHaveLength(1); });
    expect(await screen.findByText("Fetching prices; the figures update when they arrive")).toBeInTheDocument();
  });

  it("offers no refresh without a price feed, nor to a viewer", async () => {
    mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () => json(holdings),
    });
    const first = renderApp("/investments");
    await screen.findByRole("table", { name: "Holdings" });
    expect(screen.queryByRole("button", { name: "Refresh prices" })).not.toBeInTheDocument();
    // Entered by hand, nothing imported: nothing to say about an import either.
    expect(screen.queryByText(/What you hold is as of/)).not.toBeInTheDocument();
    first.unmount();

    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () => json({ ...holdings, price_feed: { enabled: true, refresh_minutes: 360 } }),
    });
    renderApp("/investments");
    await screen.findByRole("table", { name: "Holdings" });
    expect(screen.queryByRole("button", { name: "Refresh prices" })).not.toBeInTheDocument();
  });

  it("names a holding without a symbol by its name", async () => {
    const [held] = holdings.holdings;
    const plan = { ...held, security: { ...held?.security, symbol: null, name: "Example Plan Portfolio" } };
    mockApi({
      ...ownerSession(),
      "GET /api/v1/accounts": accounts(),
      "GET /api/v1/households/:id": referenceHandlers()["GET /api/v1/households/:id"] ?? (() => json({})),
      "GET /api/v1/investments/holdings": () => json({ ...holdings, holdings: [plan] }),
    });
    renderApp("/investments");
    const table = await screen.findByRole("table", { name: "Holdings" });
    const row = within(table).getByRole("row", { name: /Example Plan Portfolio/ });
    expect(within(row).getByText("Example Plan Portfolio")).toBeInTheDocument();
    expect(within(row).queryByText("null")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /Show lots of Example Plan Portfolio in Brokerage/ }));
    expect(await screen.findByRole("dialog", { name: "Lots of Example Plan Portfolio" })).toBeInTheDocument();
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

const security = (patch: Record<string, unknown> = {}) => ({
  id: SEC,
  symbol: "ACME",
  name: "Acme Crop",
  asset_class: "equity",
  currency: "USD",
  isin: null,
  cusip: null,
  sector: "technology",
  exchange: null,
  created_at: STAMP,
  updated_at: STAMP,
  ...patch,
});

const OTHER = "5e000000-0000-4000-8000-000000000002";

function pricesHandlers(extra: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    ...ownerSession(),
    "GET /api/v1/accounts": accounts(),
    "GET /api/v1/investments/holdings": () => json(holdings),
    "GET /api/v1/securities": () => json({ items: [security(), security({ id: OTHER, symbol: "ZED", name: "Zed Fund" })], next_cursor: null }),
    "GET /api/v1/securities/:id/prices": () => json({ items: [], next_cursor: null }),
    ...extra,
  };
}

async function openPrices(user: ReturnType<typeof userEvent.setup>) {
  renderApp("/investments");
  await user.click(await screen.findByRole("tab", { name: "Prices" }));
  await screen.findByRole("combobox", { name: "Security" });
}

describe("editing and deleting a security", () => {
  it("corrects a security and sends only what changed", async () => {
    const api = mockApi(pricesHandlers({ "PATCH /api/v1/securities/:id": () => json(security({ name: "Acme Corp", sector: null, isin: "US0378331005" })) }));
    const user = userEvent.setup();
    await openPrices(user);
    await user.click(screen.getByRole("button", { name: "Edit security" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit security" });
    expect(within(dialog).getByLabelText(/^Symbol/)).toHaveValue("ACME");
    const name = within(dialog).getByLabelText(/^Name/);
    await user.clear(name);
    await user.type(name, "Acme Corp");
    await user.clear(within(dialog).getByLabelText(/^Sector/));
    await user.type(within(dialog).getByLabelText(/^ISIN/), "us0378331005");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => { expect(api.callsTo("PATCH", `/api/v1/securities/${SEC}`)).toHaveLength(1); });
    const call = api.callsTo("PATCH", `/api/v1/securities/${SEC}`)[0];
    expect(call?.body).toEqual({ name: "Acme Corp", sector: null, isin: "US0378331005" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Security updated")).toBeInTheDocument();
    await vi.waitFor(() => { expect(screen.queryByRole("dialog", { name: "Edit security" })).not.toBeInTheDocument(); });
  });

  it("checks the identifiers before sending and shows a refused currency change", async () => {
    const api = mockApi(pricesHandlers({ "PATCH /api/v1/securities/:id": () => problem(409, "security-currency-locked") }));
    const user = userEvent.setup();
    await openPrices(user);
    await user.click(screen.getByRole("button", { name: "Edit security" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit security" });
    await user.type(within(dialog).getByLabelText(/^CUSIP/), "123");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByText("A CUSIP has 9 characters.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/securities/:id")).toHaveLength(0);
    await user.clear(within(dialog).getByLabelText(/^CUSIP/));
    const currency = within(dialog).getByLabelText(/^Currency/);
    await user.clear(currency);
    await user.type(currency, "eur");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByText("detail of security-currency-locked")).toBeInTheDocument();
    expect(api.callsTo("PATCH", `/api/v1/securities/${SEC}`)[0]?.body).toEqual({ currency: "EUR" });
  });

  it("closes without a request when nothing changed", async () => {
    const api = mockApi(pricesHandlers());
    const user = userEvent.setup();
    await openPrices(user);
    await user.click(screen.getByRole("button", { name: "Edit security" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit security" });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() => { expect(screen.queryByRole("dialog", { name: "Edit security" })).not.toBeInTheDocument(); });
    expect(api.callsTo("PATCH", "/api/v1/securities/:id")).toHaveLength(0);
  });

  it("deletes the chosen security after confirmation", async () => {
    const state = { deleted: false };
    const api = mockApi(
      pricesHandlers({
        "GET /api/v1/securities": () =>
          json({ items: [...(state.deleted ? [] : [security()]), security({ id: OTHER, symbol: "ZED", name: "Zed Fund" })], next_cursor: null }),
        "DELETE /api/v1/securities/:id": () => {
          state.deleted = true;
          return noContent();
        },
      }),
    );
    const user = userEvent.setup();
    await openPrices(user);
    await user.click(screen.getByRole("button", { name: "Delete security" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete ACME?" });
    expect(dialog).toHaveTextContent("A security that has transactions or lots cannot be deleted.");
    expect(api.callsTo("DELETE", "/api/v1/securities/:id")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete security" }));
    expect(await screen.findByText("Security deleted")).toBeInTheDocument();
    const calls = api.callsTo("DELETE", `/api/v1/securities/${SEC}`);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    await vi.waitFor(() => { expect(screen.getByRole("combobox", { name: "Security" })).toHaveValue(OTHER); });
    expect(screen.queryByRole("dialog", { name: /^Delete/ })).not.toBeInTheDocument();
    // Its prices went with it and are not asked for again.
    expect(api.callsTo("GET", `/api/v1/securities/${SEC}/prices`)).toHaveLength(1);
    expect(api.callsTo("GET", `/api/v1/securities/${OTHER}/prices`)).toHaveLength(1);
  });

  it("shows why a security in use cannot be deleted", async () => {
    mockApi(pricesHandlers({ "DELETE /api/v1/securities/:id": () => problem(409, "security-in-use") }));
    const user = userEvent.setup();
    await openPrices(user);
    await user.click(screen.getByRole("button", { name: "Delete security" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete ACME?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete security" }));
    expect(await within(dialog).findByText("detail of security-in-use")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Security" })).toHaveValue(SEC);
  });

  it("has no edit or delete for a viewer", async () => {
    mockApi(pricesHandlers({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }) }));
    const user = userEvent.setup();
    await openPrices(user);
    expect(screen.queryByRole("button", { name: "Edit security" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete security" })).not.toBeInTheDocument();
  });
});

describe("cost basis method", () => {
  const costHandlers = (extra: Record<string, Handler> = {}): Record<string, Handler> => ({
    ...ownerSession(),
    "GET /api/v1/accounts": accounts(),
    "GET /api/v1/investments/holdings": () => json(holdings),
    "GET /api/v1/investments/accounts/:id/cost-method": () => json({ account_id: BROKERAGE, method: "fifo" }),
    ...extra,
  });

  it("shows each investment account's method and changes it", async () => {
    const state = { method: "fifo" };
    const api = mockApi(
      costHandlers({
        "GET /api/v1/investments/accounts/:id/cost-method": () => json({ account_id: BROKERAGE, method: state.method }),
        "PUT /api/v1/investments/accounts/:id/cost-method": (call) => {
          state.method = (call.body as { method: string }).method;
          return json({ account_id: BROKERAGE, method: state.method });
        },
      }),
    );
    renderApp("/investments");
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "Cost basis method" });
    const list = within(card).getByRole("list", { name: "Cost basis method by account" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1); // the checking account is not an investment account
    expect(await within(list).findByText("First in, first out (FIFO)")).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Change the cost basis method of Brokerage" }));
    const dialog = await screen.findByRole("dialog", { name: "Cost basis method of Brokerage" });
    expect(dialog).toHaveTextContent("recalculates this account's lots, cost basis and realised gains");
    await user.selectOptions(within(dialog).getByLabelText("Method"), "average");
    expect(dialog).toHaveTextContent("A sale uses the average cost of all units held");
    await user.click(within(dialog).getByRole("button", { name: "Save method" }));
    await vi.waitFor(() => { expect(api.callsTo("PUT", `/api/v1/investments/accounts/${BROKERAGE}/cost-method`)).toHaveLength(1); });
    const call = api.callsTo("PUT", `/api/v1/investments/accounts/${BROKERAGE}/cost-method`)[0];
    expect(call?.body).toEqual({ method: "average" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await within(list).findByText("Average cost")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Cost basis method of Brokerage" })).not.toBeInTheDocument();
  });

  it("keeps the dialog open with the server's reason when the lots do not fit the method", async () => {
    mockApi(costHandlers({ "PUT /api/v1/investments/accounts/:id/cost-method": () => problem(422, "lot-selection-not-allowed") }));
    renderApp("/investments");
    const user = userEvent.setup();
    const card = await screen.findByRole("region", { name: "Cost basis method" });
    await user.click(await within(card).findByRole("button", { name: "Change the cost basis method of Brokerage" }));
    const dialog = await screen.findByRole("dialog", { name: "Cost basis method of Brokerage" });
    await user.selectOptions(within(dialog).getByLabelText("Method"), "average");
    await user.click(within(dialog).getByRole("button", { name: "Save method" }));
    expect(await within(dialog).findByText("detail of lot-selection-not-allowed")).toBeInTheDocument();
    expect(within(card).getByText("First in, first out (FIFO)", { selector: "span" })).toBeInTheDocument(); // the row, not the dialog's option
  });

  it("is read only on an account the person cannot edit", async () => {
    const readOnly: Handler = async (call) => {
      const body = (await (await accounts()(call)).json()) as { items: Record<string, unknown>[] };
      return json({ items: body.items.map((entry) => ({ ...entry, can_edit: false })), next_cursor: null });
    };
    mockApi(costHandlers({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }), "GET /api/v1/accounts": readOnly }));
    renderApp("/investments");
    const card = await screen.findByRole("region", { name: "Cost basis method" });
    expect(await within(card).findByText("First in, first out (FIFO)")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /Change the cost basis method/ })).not.toBeInTheDocument();
  });
});
