// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { formatDate, todayIsoDate } from "../../lib/dates";
import { authedSession, json, mockApi, noContent, renderApp, type Handler } from "../auth/testing";
import { ownerSession, referenceHandlers } from "../planning/testing";
import { buildFilter, buildRate, cleanRate, inverseInWords, listInWords, rateInWords } from "./fx";
import type { FxRate } from "./fxApi";

const STAMP = "2026-11-03T10:00:00Z";
const TODAY = todayIsoDate();
const day = (iso: string): string => formatDate(iso) ?? iso;

const rate = (id: string, base: string, quote: string, date: string, value: string, source = "manual"): FxRate => ({
  id,
  base,
  quote,
  rate_date: date,
  rate: value,
  source,
  created_at: STAMP,
  updated_at: STAMP,
});

const RATES = [rate("f-2", "EUR", "USD", "2026-10-01", "1.0812"), rate("f-1", "GBP", "USD", "2026-09-30", "1.31", "ecb")];

function fxApi(session: Record<string, Handler>, extra: Record<string, Handler> = {}) {
  const state = { rates: [...RATES] };
  const api = mockApi({
    ...session,
    ...referenceHandlers(),
    "GET /api/v1/fx-rates": () => json({ items: state.rates, next_cursor: null }),
    ...extra,
  });
  return { api, state };
}

describe("exchange rate helpers", () => {
  it("builds the request from the form and names what is wrong", () => {
    const form = { base: " eur ", quote: "usd", date: "2026-11-01", rate: "1,08" };
    expect(buildRate(form, "2026-11-03")).toEqual({ body: { base: "EUR", quote: "USD", rate_date: "2026-11-01", rate: "1.08" }, errors: {} });
    const errors = (patch: Partial<typeof form>) => buildRate({ ...form, ...patch }, "2026-11-03").errors;
    expect(errors({ base: "EU" })["base"]).toMatch(/three-letter/);
    expect(errors({ quote: "EUR" })["quote"]).toBe("Choose two different currencies.");
    expect(errors({ date: "2026-11-04" })["rate_date"]).toBe("A rate cannot be dated in the future.");
    expect(errors({ date: "" })["rate_date"]).toBeDefined();
    expect(errors({ rate: "" })["rate"]).toBe("Enter the rate.");
    expect(errors({ rate: "0.000" })["rate"]).toBe("The rate must be above zero.");
    expect(errors({ rate: "-1" })["rate"]).toMatch(/plain number/);
    expect(errors({ rate: "1.2.3" })["rate"]).toMatch(/plain number/);
    expect(errors({ rate: "1.1234567890123" })["rate"]).toMatch(/plain number/);
    expect(cleanRate(" 1,0812 ")).toBe("1.0812");
    expect(cleanRate("1,000.5")).toBe("1,000.5");
  });

  it("says a rate in words, both ways round", () => {
    expect(rateInWords({ base: "EUR", quote: "USD", rate: "1.08" })).toBe("1 EUR = 1.08 USD");
    expect(inverseInWords({ base: "EUR", quote: "USD", rate: "1.08" })).toBe("1 USD is about 0.9259 EUR");
    expect(inverseInWords({ base: "EUR", quote: "USD", rate: "nope" })).toBeNull();
    expect(listInWords(["EUR"])).toBe("EUR");
    expect(listInWords(["EUR", "GBP", "JPY"])).toBe("EUR, GBP and JPY");
    expect(buildFilter({ base: "eur", quote: "" })).toEqual({ ok: true, pair: { base: "EUR", quote: "" } });
    expect(buildFilter({ base: "e", quote: "" }).ok).toBe(false);
  });
});

describe("exchange rates page", () => {
  it("is listed in settings and shows the recorded rates with their source", async () => {
    fxApi(ownerSession());
    renderApp("/settings");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Exchange rates" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Exchange rates" })).toBeInTheDocument();
    const table = await screen.findByRole("region", { name: "Recorded exchange rates" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(day("2026-10-01"));
    expect(rows[0]).toHaveTextContent("EUR to USD");
    expect(rows[0]).toHaveTextContent("1 EUR = 1.0812 USD");
    expect(rows[0]).toHaveTextContent("Entered by hand");
    expect(rows[1]).toHaveTextContent("Fetched (ecb)");
    // Only a rate somebody entered can be deleted here.
    expect(screen.getByRole("button", { name: `Delete the EUR to USD rate from ${day("2026-10-01")}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete the GBP to USD rate/ })).not.toBeInTheDocument();
    expect(screen.getByText(/latest rate on or before its own date/)).toBeInTheDocument();
  });

  it("reads a new rate back in words and saves it against the base currency", async () => {
    const { api, state } = fxApi(ownerSession(), {
      "GET /api/v1/accounts": () =>
        json({
          items: [
            { id: "a-1", name: "Checking", nickname: null, currency: "USD" },
            { id: "a-2", name: "Euro savings", nickname: null, currency: "EUR" },
          ],
          next_cursor: null,
        }),
      "POST /api/v1/fx-rates": (call) => {
        const body = call.body as { base: string; quote: string; rate_date: string; rate: string };
        const saved = rate("f-3", body.base, body.quote, body.rate_date, body.rate);
        state.rates = [saved, ...state.rates];
        return json(saved, 201);
      },
    });
    renderApp("/settings/exchange-rates");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Add a rate" });
    expect(screen.getByRole("note")).toHaveTextContent("Your accounts hold EUR besides USD, so that currency needs a rate to USD.");
    // The foreign currency and the base currency are filled in.
    expect(within(form).getByLabelText(/^Currency/)).toHaveValue("EUR");
    expect(within(form).getByLabelText(/^Priced in/)).toHaveValue("USD");
    expect(within(form).getByLabelText(/^Date/)).toHaveValue(TODAY);
    await user.click(within(form).getByRole("button", { name: "Save rate" }));
    expect(await within(form).findByText("Enter the rate.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/fx-rates")).toHaveLength(0);

    await user.type(within(form).getByLabelText(/^Rate/), "1,08");
    expect(screen.getByTestId("rate-in-words")).toHaveTextContent(`1 EUR = 1.08 USD from ${day(TODAY)} (1 USD is about 0.9259 EUR).`);
    await user.click(within(form).getByRole("button", { name: "Save rate" }));
    expect(await screen.findByText("Rate saved")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/fx-rates");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toEqual({ base: "EUR", quote: "USD", rate_date: TODAY, rate: "1.08" });
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(calls[0]?.headers.get("Idempotency-Key")).toMatch(/^[\x21-\x7e]{16,255}$/);
    expect(await screen.findByRole("button", { name: `Delete the EUR to USD rate from ${day(TODAY)}` })).toBeInTheDocument();
    expect(within(form).getByLabelText(/^Rate/)).toHaveValue("");
  });

  it("shows the server's refusal and keeps what was typed", async () => {
    let attempt = 0;
    const { api } = fxApi(ownerSession(), {
      "POST /api/v1/fx-rates": () => {
        attempt += 1;
        return attempt === 1
          ? json(
              {
                type: "urn:perfin:problem:fx-rate-date-in-future",
                title: "Rate date in the future",
                status: 422,
                detail: "Exchange rates are recorded for days that have begun.",
              },
              422,
            )
          : json(
              {
                type: "urn:perfin:problem:validation-failed",
                title: "Request validation failed",
                status: 422,
                detail: "One or more fields are invalid.",
                errors: [{ loc: ["body", "base"], msg: "Value error, must be an active ISO 4217 currency code such as USD", type: "value_error" }],
              },
              422,
            );
      },
    });
    renderApp("/settings/exchange-rates");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Add a rate" });
    await user.type(within(form).getByLabelText(/^Currency/), "zzz");
    await user.type(within(form).getByLabelText(/^Rate/), "2");
    await user.click(within(form).getByRole("button", { name: "Save rate" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Exchange rates are recorded for days that have begun.");
    expect(within(form).getByLabelText(/^Rate/)).toHaveValue("2");

    await user.click(within(form).getByRole("button", { name: "Save rate" }));
    expect(await within(form).findByText("Must be an active ISO 4217 currency code such as USD.")).toBeInTheDocument();
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    // The same request again carries the same key.
    const keys = api.callsTo("POST", "/api/v1/fx-rates").map((call) => call.headers.get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    expect(screen.queryByText("Rate saved")).not.toBeInTheDocument();
  });

  it("filters by pair in the direction the rates were recorded", async () => {
    const { api } = fxApi(ownerSession(), {
      "GET /api/v1/fx-rates": (call) =>
        json({ items: call.search.includes("base=USD") ? [] : call.search.includes("base=EUR") ? [RATES[0]] : RATES, next_cursor: null }),
    });
    renderApp("/settings/exchange-rates");
    const user = userEvent.setup();
    const filter = await screen.findByRole("search", { name: "Filter rates by pair" });
    await screen.findByText("GBP to USD");
    await user.type(within(filter).getByLabelText("Show currency"), "e");
    await user.click(within(filter).getByRole("button", { name: "Filter" }));
    expect(await within(filter).findByText(/three-letter currency code/)).toBeInTheDocument();
    await user.type(within(filter).getByLabelText("Show currency"), "ur");
    await user.type(within(filter).getByLabelText("Show priced in"), "usd");
    await user.click(within(filter).getByRole("button", { name: "Filter" }));
    await waitFor(() => {
      expect(screen.queryByText("GBP to USD")).not.toBeInTheDocument();
    });
    expect(screen.getByText("EUR to USD")).toBeInTheDocument();
    const last = api.callsTo("GET", "/api/v1/fx-rates").at(-1);
    expect(last?.search).toContain("base=EUR");
    expect(last?.search).toContain("quote=USD");

    await user.clear(within(filter).getByLabelText("Show currency"));
    await user.type(within(filter).getByLabelText("Show currency"), "usd");
    await user.clear(within(filter).getByLabelText("Show priced in"));
    await user.click(within(filter).getByRole("button", { name: "Filter" }));
    expect(await screen.findByText("No rates for this pair")).toBeInTheDocument();
    await user.click(within(filter).getByRole("button", { name: "Show all pairs" }));
    expect(await screen.findByText("GBP to USD")).toBeInTheDocument();
  });

  it("deletes a rate entered by hand after a confirmation, and shows a refusal", async () => {
    let refuse = true;
    const { api, state } = fxApi(ownerSession(), {
      "DELETE /api/v1/fx-rates/:id": () => {
        if (refuse) {
          return json({ type: "urn:perfin:problem:fx-rate-not-found", title: "Exchange rate not found", status: 404 }, 404);
        }
        state.rates = state.rates.filter((item) => item.id !== "f-2");
        return noContent();
      },
    });
    renderApp("/settings/exchange-rates");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: `Delete the EUR to USD rate from ${day("2026-10-01")}` }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this rate?" });
    expect(dialog).toHaveTextContent(`1 EUR = 1.0812 USD from ${day("2026-10-01")}.`);
    expect(dialog).toHaveTextContent(/converted with the rate before it/);
    await user.click(within(dialog).getByRole("button", { name: "Delete rate" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Exchange rate not found");

    refuse = false;
    await user.click(within(dialog).getByRole("button", { name: "Delete rate" }));
    expect(await screen.findByText("Rate deleted")).toBeInTheDocument();
    const calls = api.callsTo("DELETE", "/api/v1/fx-rates/:id");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.path).toBe("/api/v1/fx-rates/f-2");
    expect(calls[1]?.headers.get("X-CSRF-Token")).toBe("session-token");
    await waitFor(() => {
      expect(screen.queryByText("EUR to USD")).not.toBeInTheDocument();
    });
    expect(screen.getByText("GBP to USD")).toBeInTheDocument();
  });

  it("lets a member read the rates but not change them", async () => {
    const { api } = fxApi({ "GET /api/v1/auth/session": authedSession({ role: "member" }) });
    renderApp("/settings/exchange-rates");
    expect(await screen.findByText("EUR to USD")).toBeInTheDocument();
    expect(screen.getByText(/Only a household owner or administrator can add or delete exchange rates/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add a rate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete/ })).not.toBeInTheDocument();
    expect(api.calls.some((call) => call.method !== "GET")).toBe(false);
  });

  it("explains an empty list", async () => {
    fxApi(ownerSession(), { "GET /api/v1/fx-rates": () => json({ items: [], next_cursor: null }) });
    renderApp("/settings/exchange-rates");
    expect(await screen.findByText("No exchange rates yet")).toBeInTheDocument();
    expect(screen.getByText("Add one above for each currency you hold other than your base currency.")).toBeInTheDocument();
  });
});
