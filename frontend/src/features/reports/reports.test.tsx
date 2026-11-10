// SPDX-License-Identifier: AGPL-3.0-or-later
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { todayIsoDate } from "../../lib/dates";
import { currentHref, json, mockApi, problem, renderApp, type ApiCall, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, CAT_RENT, money, ownerSession, referenceHandlers } from "../planning/testing";
import { monthOf, presetRange } from "./periods";
import { cashflowReport, flowReport, trendReport } from "./testing";

const saved = vi.hoisted(() => ({ saveBlob: vi.fn() }));
vi.mock("../settings/saveFile", async (importOriginal) => {
  const original = await importOriginal<typeof import("../settings/saveFile")>();
  return { ...original, saveBlob: saved.saveBlob };
});

const SPENDING = "/api/v1/reports/spending";
const INCOME = "/api/v1/reports/income";
const CASHFLOW = "/api/v1/reports/cashflow";
const TRENDS = "/api/v1/reports/trends";

function reportsApi(handlers: Record<string, Handler> = {}) {
  return mockApi({
    ...ownerSession(),
    ...referenceHandlers(),
    [`GET ${SPENDING}`]: () => json(flowReport()),
    [`GET ${INCOME}`]: () => json(flowReport({ flow: "income", items: [{ key: "c-pay", name: "Salary", amount: money("3000.00") }], total: money("3000.00") })),
    [`GET ${CASHFLOW}`]: () => json(cashflowReport()),
    [`GET ${TRENDS}`]: () => json(trendReport()),
    ...handlers,
  });
}

/** The query of the latest call to `path`, as plain pairs (a repeated key keeps its last value). */
function lastQuery(api: { callsTo: (method: string, path: string) => ApiCall[] }, path: string): Record<string, string> {
  const call = api.callsTo("GET", path).at(-1);
  return Object.fromEntries(new URLSearchParams(call?.search ?? ""));
}

async function savedCsv(): Promise<{ text: string; bytes: Uint8Array; filename: string }> {
  await waitFor(() => {
    expect(saved.saveBlob).toHaveBeenCalledTimes(1);
  });
  const [blob, filename] = saved.saveBlob.mock.calls[0] as [Blob, string];
  return { text: await blob.text(), bytes: new Uint8Array(await blob.arrayBuffer()), filename };
}

beforeEach(() => {
  saved.saveBlob.mockClear();
});

describe("reports page: spending and income", () => {
  it("shows this month's spending by category with a chart, a table with totals and links to the transactions", async () => {
    const api = reportsApi();
    renderApp("/reports");
    expect(await screen.findByRole("heading", { level: 1, name: "Reports" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Spending by category, 3 bars" })).toBeInTheDocument();

    // The default period is the calendar month, like the dashboard; pending rows follow the household setting.
    const month = presetRange("this-month", todayIsoDate());
    expect(lastQuery(api, SPENDING)).toEqual({ date_from: month.from, date_to: month.to, group_by: "category", depth: "1" });
    expect(api.callsTo("GET", INCOME)).toHaveLength(0);
    expect(api.callsTo("GET", CASHFLOW)).toHaveLength(0);
    expect(screen.getByRole("tab", { name: "Spending" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/for all accounts\. Transactions awaiting review are left out\./)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Include transactions awaiting review" })).not.toBeChecked();

    const figures = screen.getByText("Total spending").closest("dl") as HTMLElement;
    expect(within(figures).getByText("$1,800.00")).toBeInTheDocument();
    expect(within(figures).getByText("Largest category")).toBeInTheDocument();
    expect(within(figures).getByText("Rent")).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Spending by category, Nov 1, 2026 to Nov 30, 2026" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Category", "Amount", "Share of total"]);
    const rent = within(table).getByRole("row", { name: /Rent/ });
    expect(within(rent).getByText("$1,450.00")).toBeInTheDocument();
    expect(within(rent).getByText("80.6%")).toBeInTheDocument();
    const total = within(table).getByRole("row", { name: /Total/ });
    expect(within(total).getByText("$1,800.00")).toBeInTheDocument();
    expect(within(total).getByText("100.0%")).toBeInTheDocument();

    // A category leads to Transactions with the report's dates; uncategorised money has its own filter value.
    const rentLink = new URL(within(table).getByRole("link", { name: "Rent" }).getAttribute("href") ?? "", "http://localhost");
    expect(rentLink.pathname).toBe("/transactions");
    expect(Object.fromEntries(rentLink.searchParams)).toEqual({ category: CAT_RENT, date_from: "2026-11-01", date_to: "2026-11-30" });
    expect(within(table).getByRole("link", { name: "Uncategorised" }).getAttribute("href")).toContain("category=none");
  });

  it("asks for a new report when the period, grouping, account or review box changes, and keeps them in the address", async () => {
    const api = reportsApi();
    const { router } = renderApp("/reports");
    const user = userEvent.setup();
    await screen.findByRole("img", { name: "Spending by category, 3 bars" });
    const year = Number(todayIsoDate().slice(0, 4));

    await user.selectOptions(screen.getByLabelText("Period"), "Last year");
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ date_from: `${String(year - 1)}-01-01`, date_to: `${String(year - 1)}-12-31` });
    });

    await user.selectOptions(screen.getByLabelText("Category detail"), "Subcategories");
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ group_by: "category", depth: "2" });
    });

    await user.selectOptions(screen.getByLabelText("Group by"), "Payee");
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ group_by: "payee" });
    });
    // Category detail only applies to categories.
    expect(lastQuery(api, SPENDING)).not.toHaveProperty("depth");
    expect(screen.queryByLabelText("Category detail")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Account"), "Checking");
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ account: ACCOUNT_CHECKING });
    });
    expect(lastQuery(api, SPENDING)).not.toHaveProperty("include_pending");
    expect(await screen.findByText(/for Checking\./)).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Include transactions awaiting review" }));
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ include_pending: "true" });
    });
    expect(lastQuery(api, SPENDING)).toEqual({
      date_from: `${String(year - 1)}-01-01`,
      date_to: `${String(year - 1)}-12-31`,
      group_by: "payee",
      account: ACCOUNT_CHECKING,
      include_pending: "true",
    });
    const href = currentHref(router);
    for (const part of ["period=last-year", "group=payee", "depth=2", `account=${ACCOUNT_CHECKING}`, "pending=true"]) {
      expect(href).toContain(part);
    }

    // The same filters carry over to the other tabs.
    await user.click(screen.getByRole("tab", { name: "Income" }));
    await waitFor(() => {
      expect(lastQuery(api, INCOME)).toEqual(lastQuery(api, SPENDING));
    });
    expect(await screen.findByRole("heading", { level: 2, name: "Income by category" })).toBeInTheDocument();
  });

  it("takes custom dates, and does not ask the server for a period it would refuse", async () => {
    const api = reportsApi();
    renderApp("/reports?period=last-month");
    const user = userEvent.setup();
    await screen.findByRole("img", { name: "Spending by category, 3 bars" });
    const lastMonth = presetRange("last-month", todayIsoDate());

    // Custom dates start from the dates on screen.
    await user.selectOptions(screen.getByLabelText("Period"), "Custom dates");
    expect(await screen.findByLabelText("From")).toHaveValue(lastMonth.from);
    expect(screen.getByLabelText("To")).toHaveValue(lastMonth.to);

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-03-05" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-04-10" } });
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ date_from: "2026-03-05", date_to: "2026-04-10" });
    });
    expect(screen.getByText(/Showing Mar 5, 2026 to Apr 10, 2026/)).toBeInTheDocument();

    const before = api.callsTo("GET", SPENDING).length;
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-03-01" } });
    expect(await screen.findByRole("heading", { name: "Choose the dates" })).toBeInTheDocument();
    expect(screen.getAllByText("The start date is after the end date.").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("To")).toHaveAccessibleDescription("The start date is after the end date.");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2010-01-01" } });
    expect((await screen.findAllByText("A report covers at most ten years. Choose a shorter period.")).length).toBeGreaterThan(0);
    expect(api.callsTo("GET", SPENDING)).toHaveLength(before);
  });

  it("groups by month and by account, shows income, and says when there is nothing", async () => {
    const byMonth = flowReport({
      group_by: "month",
      date_from: "2026-10-01",
      items: [
        { key: "2026-10", name: null, amount: money("2000.00") },
        { key: "2026-11", name: null, amount: money("1800.00") },
      ],
      total: money("3800.00"),
    });
    const byAccount = flowReport({ group_by: "account", items: [{ key: ACCOUNT_CHECKING, name: "Checking", amount: money("1800.00") }] });
    const api = reportsApi({
      [`GET ${SPENDING}`]: (call) => json(call.search.includes("group_by=month") ? byMonth : call.search.includes("group_by=account") ? byAccount : flowReport()),
      [`GET ${INCOME}`]: () => json(flowReport({ flow: "income", items: [], total: money("0.00") })),
    });
    renderApp("/reports?group=month");
    const user = userEvent.setup();
    expect(await screen.findByRole("img", { name: "Spending by month, 2 months" })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: /^Spending by month/ });
    expect(within(table).getByRole("rowheader", { name: "October 2026" })).toBeInTheDocument();
    expect(within(table).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Highest month")).toBeInTheDocument();

    // One account is one figure, not a chart; its name opens that account's transactions.
    await user.selectOptions(screen.getByLabelText("Group by"), "Account");
    const accounts = await screen.findByRole("table", { name: /^Spending by account/ });
    expect(screen.queryByRole("img", { name: /Spending by account/ })).not.toBeInTheDocument();
    const link = new URL(within(accounts).getByRole("link", { name: "Checking" }).getAttribute("href") ?? "", "http://localhost");
    expect(Object.fromEntries(link.searchParams)).toEqual({ account: ACCOUNT_CHECKING, date_from: "2026-11-01", date_to: "2026-11-30" });

    await user.click(screen.getByRole("tab", { name: "Income" }));
    expect(await screen.findByRole("heading", { name: "No income in this period" })).toBeInTheDocument();
    expect(screen.getByText(/Transactions still waiting in Review are left out unless you include them above\./)).toBeInTheDocument();
    expect(lastQuery(api, INCOME)).toMatchObject({ group_by: "account" });
  });

  it("shows amounts the server could not convert instead of hiding them, and follows the household's review setting", async () => {
    const api = reportsApi({
      [`GET ${SPENDING}`]: () => json(flowReport({ include_pending: true, unconverted: [money("120.00", "EUR"), money("30.00", "GBP")] })),
    });
    renderApp("/reports");
    const note = await screen.findByText(/could not be converted to USD/);
    expect(note).toHaveAttribute("role", "status");
    expect(within(note).getByText("€120.00")).toBeInTheDocument();
    expect(within(note).getByText("£30.00")).toBeInTheDocument();
    expect(note).toHaveTextContent("of spending could not be converted to USD because there is no exchange rate for the day. They are left out of the totals, the chart and the table.");
    // The total is the server's, without the unconverted amounts.
    expect(within(screen.getByText("Total spending").closest("dl") as HTMLElement).getByText("$1,800.00")).toBeInTheDocument();

    // Nothing was asked about pending rows; the report says the household includes them.
    expect(lastQuery(api, SPENDING)).not.toHaveProperty("include_pending");
    expect(screen.getByRole("checkbox", { name: "Include transactions awaiting review" })).toBeChecked();
    expect(screen.getByText(/Transactions awaiting review are included\./)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("checkbox", { name: "Include transactions awaiting review" }));
    await waitFor(() => {
      expect(lastQuery(api, SPENDING)).toMatchObject({ include_pending: "false" });
    });
  });

  it("keeps a report with only unconverted amounts on screen", async () => {
    reportsApi({ [`GET ${SPENDING}`]: () => json(flowReport({ items: [], total: money("0.00"), unconverted: [money("75.00", "EUR")] })) });
    renderApp("/reports");
    expect(await screen.findByText(/could not be converted to USD/)).toHaveTextContent("It is left out of the totals");
    expect(screen.queryByRole("heading", { name: "No spending in this period" })).not.toBeInTheDocument();
    expect(screen.getByText("No spending was converted for this period.")).toBeInTheDocument();
  });

  it("shows the server's message when a report cannot be loaded", async () => {
    const api = reportsApi({ [`GET ${SPENDING}`]: () => problem(404, "account-not-found") });
    renderApp(`/reports?account=${ACCOUNT_CHECKING}`);
    const alert = await screen.findByText(/Could not load the report\./);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("Could not load the report. detail of account-not-found");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // The filters stay usable, and another tab can still load.
    expect(screen.getByLabelText("Period")).toBeEnabled();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Cash flow" }));
    expect(await screen.findByRole("table", { name: /^Cash flow by month/ })).toBeInTheDocument();
    expect(lastQuery(api, CASHFLOW)).toMatchObject({ account: ACCOUNT_CHECKING });
  });
});

describe("reports page: cash flow", () => {
  it("shows money in, money out and net per month with totals", async () => {
    const api = reportsApi();
    renderApp("/reports?tab=cashflow&period=last-12-months");
    expect(await screen.findByRole("img", { name: "Money in, money out and net by month, 2 months" })).toBeInTheDocument();
    const range = presetRange("last-12-months", todayIsoDate());
    expect(lastQuery(api, CASHFLOW)).toEqual({ date_from: range.from, date_to: range.to });
    expect(api.callsTo("GET", SPENDING)).toHaveLength(0);

    const figures = screen.getByText("Money in", { selector: "dt" }).closest("dl") as HTMLElement;
    expect(within(figures).getByText("$6,466.18")).toBeInTheDocument();
    expect(within(figures).getByText("$5,990.67")).toBeInTheDocument();
    expect(within(figures).getByText("+$475.51")).toBeInTheDocument();

    const key = screen.getByRole("list", { name: "Chart key" });
    expect(within(key).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Money in", "Money out", "Net (in minus out)"]);

    const table = screen.getByRole("table", { name: "Cash flow by month, Oct 1, 2026 to Nov 30, 2026" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Month", "Money in", "Money out", "Net"]);
    const october = within(table).getByRole("row", { name: /October 2026/ });
    expect(within(october).getByText("$3,466.18")).toBeInTheDocument();
    expect(within(october).getByText("$3,667.08")).toBeInTheDocument();
    expect(within(october).getByText("-$200.90")).toBeInTheDocument();
    const total = within(table).getByRole("row", { name: /Total/ });
    expect(within(total).getByText("$6,466.18")).toBeInTheDocument();
    expect(within(total).getByText("+$475.51")).toBeInTheDocument();
  });

  it("shows an unconverted net amount and says when nothing moved", async () => {
    const api = reportsApi({ [`GET ${CASHFLOW}`]: () => json(cashflowReport({ unconverted: [money("-80.00", "GBP")] })) });
    renderApp("/reports?tab=cashflow");
    const note = await screen.findByText(/could not be converted to USD/);
    expect(within(note).getByText("-£80.00")).toBeInTheDocument();
    expect(note).toHaveTextContent("of net cash flow (a minus sign means money out)");

    api.on(`GET ${CASHFLOW}`, () =>
      json(cashflowReport({ months: [{ month: "2026-10", inflow: money("0.00"), outflow: money("0.00"), net: money("0.00") }], inflow: money("0.00"), outflow: money("0.00"), net: money("0.00") })),
    );
    await userEvent.setup().selectOptions(screen.getByLabelText("Period"), "Last month");
    expect(await screen.findByRole("heading", { name: "No money in or out in this period" })).toBeInTheDocument();
  });
});

describe("reports page: trends", () => {
  it("compares the period's last month with the month before and a year earlier", async () => {
    const api = reportsApi();
    renderApp("/reports?tab=trends&period=custom&from=2026-10-10&to=2026-11-15");
    expect(await screen.findByRole("img", { name: "Spending month by month, 3 months" })).toBeInTheDocument();
    // Whole months: the month the period ends in, thirteen months of history by default.
    expect(lastQuery(api, TRENDS)).toEqual({ flow: "spending", month: "2026-11", months: "13", depth: "1" });

    expect(screen.getByText(/^November 2026 against the month before and the same month a year earlier\./)).toBeInTheDocument();
    const figures = screen.getByText("Spending in November 2026").closest("dl") as HTMLElement;
    expect(within(figures).getByText("$1,800.00")).toBeInTheDocument();
    expect(within(figures).getByText("Change from October 2026")).toBeInTheDocument();
    expect(within(figures).getByText("-$200.00")).toBeInTheDocument();
    expect(within(figures).getByText("-10.0%")).toBeInTheDocument();
    expect(within(figures).getByText("Change from November 2025")).toBeInTheDocument();
    expect(within(figures).getByText("+$1,800.00")).toBeInTheDocument();
    // No percentage can be worked out from zero; the page says so instead of inventing one.
    expect(within(figures).getByText("Nothing in November 2025 to compare with")).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Spending in November 2026 by category, compared with October 2026 and November 2025" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Category", "Nov 2026", "Oct 2026", "Month on month", "Nov 2025", "Year on year"]);
    const food = within(table).getByRole("row", { name: /Food/ });
    expect(within(food).getByText("$350.00")).toBeInTheDocument();
    expect(within(food).getByText("$550.00")).toBeInTheDocument();
    expect(within(food).getByText("-$200.00")).toBeInTheDocument();
    expect(within(food).getByText("-36.4%")).toBeInTheDocument();
    expect(within(food).getByText("+$350.00")).toBeInTheDocument();
    expect(within(food).getByText("No percentage")).toBeInTheDocument();
    const total = within(table).getByRole("row", { name: /Total/ });
    expect(within(total).getByText("$2,000.00")).toBeInTheDocument();
    expect(within(total).getByText("-10.0%")).toBeInTheDocument();

    // The chart's own figures are available as a table too.
    const series = screen.getByRole("table", { name: "Spending month by month", hidden: true });
    expect(within(series).getByRole("rowheader", { name: "September 2026", hidden: true })).toBeInTheDocument();
    expect(within(series).getByText("$5,300.00")).toBeInTheDocument();
  });

  it("switches to income, another category level and a longer history", async () => {
    const api = reportsApi({ [`GET ${TRENDS}`]: (call) => json(trendReport({ flow: call.search.includes("flow=income") ? "income" : "spending" })) });
    const { router } = renderApp("/reports?tab=trends&period=last-year");
    const user = userEvent.setup();
    await screen.findByRole("img", { name: "Spending month by month, 3 months" });
    const lastDecember = `${String(Number(todayIsoDate().slice(0, 4)) - 1)}-12`;
    expect(lastQuery(api, TRENDS)).toMatchObject({ month: lastDecember });

    await user.selectOptions(screen.getByLabelText("Compare"), "Income");
    expect(await screen.findByRole("heading", { level: 2, name: "Income month by month" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Category detail"), "Most detailed");
    await user.selectOptions(screen.getByLabelText("Months in the chart"), "25 months");
    await waitFor(() => {
      expect(lastQuery(api, TRENDS)).toEqual({ flow: "income", month: lastDecember, months: "25", depth: "3" });
    });
    expect(currentHref(router)).toContain("flow=income");
    expect(currentHref(router)).toContain("months=25");
  });

  it("says when the month compared is still running, and when there is nothing to compare", async () => {
    const thisMonth = monthOf(todayIsoDate());
    const api = reportsApi({ [`GET ${TRENDS}`]: () => json(trendReport({ month: thisMonth, series: [{ month: thisMonth, amount: money("5.00") }] })) });
    renderApp("/reports?tab=trends");
    expect(await screen.findByText(/is not over yet, so its figures can still grow\./)).toBeInTheDocument();

    api.on(`GET ${TRENDS}`, () => json(trendReport({ series: [{ month: "2026-11", amount: money("0.00") }], categories: [] })));
    await userEvent.setup().selectOptions(screen.getByLabelText("Months in the chart"), "6 months");
    expect(await screen.findByRole("heading", { name: "No spending to compare" })).toBeInTheDocument();
  });
});

describe("reports page: csv download", () => {
  it("downloads the table in view, with bank-supplied names made safe for spreadsheets", async () => {
    reportsApi({
      [`GET ${SPENDING}`]: () =>
        json(
          flowReport({
            group_by: "payee",
            items: [
              { key: "p-1", name: '=HYPERLINK("http://evil.example","Refund")', amount: money("90.00") },
              { key: "p-2", name: "+1 800 555 0100", amount: money("15.00") },
              { key: "p-3", name: "@home, Inc", amount: money("5.00") },
              { key: null, name: null, amount: money("-10.00") },
            ],
            total: money("100.00"),
            unconverted: [money("12.00", "EUR")],
          }),
        ),
    });
    renderApp("/reports?group=payee");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Download CSV" }));
    const file = await savedCsv();
    expect(file.filename).toBe("perfin-spending-by-payee-2026-11-01-to-2026-11-30.csv");
    // A byte-order mark so spreadsheets read accented names as UTF-8.
    expect([...file.bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    expect(file.text.replace(/^\uFEFF/, "")).toBe(
      [
        "Payee,Amount,Currency,Share of total (%)",
        '"\'=HYPERLINK(""http://evil.example"",""Refund"")",90.00,USD,90.0',
        "'+1 800 555 0100,15.00,USD,15.0",
        '"\'@home, Inc",5.00,USD,5.0',
        "No payee,-10.00,USD,-10.0",
        "Total,100.00,USD,100.0",
        "Not converted (no exchange rate),12.00,EUR,",
        "",
      ].join("\r\n"),
    );
  });

  it("downloads cash flow and both trend tables", async () => {
    reportsApi();
    renderApp("/reports?tab=cashflow");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Download CSV" }));
    const cashflow = await savedCsv();
    expect(cashflow.filename).toBe("perfin-cash-flow-2026-10-01-to-2026-11-30.csv");
    expect(cashflow.text).toContain("Month,Money in,Money out,Net,Currency\r\n2026-10,3466.18,3667.08,-200.90,USD\r\n");
    expect(cashflow.text).toContain("Total,6466.18,5990.67,475.51,USD\r\n");

    saved.saveBlob.mockClear();
    await user.click(screen.getByRole("tab", { name: "Trends" }));
    await user.click(await screen.findByRole("button", { name: "Download CSV of the comparison by category" }));
    const comparison = await savedCsv();
    expect(comparison.filename).toBe("perfin-spending-trends-2026-11.csv");
    expect(comparison.text).toContain("Food,350.00,550.00,-200.00,-36.36,0.00,350.00,,USD\r\n");

    saved.saveBlob.mockClear();
    await user.click(screen.getByRole("button", { name: "Download CSV of the monthly totals" }));
    const series = await savedCsv();
    expect(series.filename).toBe("perfin-spending-by-month-2026-09-to-2026-11.csv");
    expect(series.text).toContain("Month,Spending,Currency\r\n2026-09,1500.00,USD\r\n");
  });
});
