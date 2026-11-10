// SPDX-License-Identifier: AGPL-3.0-or-later
import { CAT_RENT, money } from "../planning/testing";
import { cashflowCsv, csvCell, csvFilename, flowCsv, seriesCsv, toCsv, trendCsv } from "./csv";
import { cashflowReport, flowReport, trendReport } from "./testing";

describe("csv cells", () => {
  it("neutralises cells a spreadsheet would run as a formula", () => {
    expect(csvCell("=SUM(A1:A9)")).toBe("'=SUM(A1:A9)");
    expect(csvCell("+1-800-555-0100")).toBe("'+1-800-555-0100");
    expect(csvCell("-2+3")).toBe("'-2+3");
    expect(csvCell("@import")).toBe("'@import");
    expect(csvCell("\tcmd")).toBe("'\tcmd");
    expect(csvCell("\r=1")).toBe("\"'\r=1\"");
    // Leading spaces do not hide a formula from importers that trim.
    expect(csvCell("  =1+1")).toBe("'  =1+1");
    // A formula with a comma or a quote is neutralised and then quoted.
    expect(csvCell('=HYPERLINK("http://x","y")')).toBe('"\'=HYPERLINK(""http://x"",""y"")"');
  });

  it("leaves plain numbers alone so negative amounts stay numbers", () => {
    expect(csvCell("-12.50")).toBe("-12.50");
    expect(csvCell("-36")).toBe("-36");
    expect(csvCell("0.00")).toBe("0.00");
    expect(csvCell("1450.00")).toBe("1450.00");
    // Only digits with an optional minus and fraction count as a number.
    expect(csvCell("+5")).toBe("'+5");
    expect(csvCell("-1e9")).toBe("'-1e9");
    expect(csvCell("-")).toBe("'-");
    expect(csvCell("-12.50 USD")).toBe("'-12.50 USD");
  });

  it("quotes commas, quotes, line breaks and outer spaces, and leaves ordinary text as it is", () => {
    expect(csvCell("Rent")).toBe("Rent");
    expect(csvCell("Smith, Jones & Co")).toBe('"Smith, Jones & Co"');
    expect(csvCell('The "Corner" Cafe')).toBe('"The ""Corner"" Cafe"');
    expect(csvCell("Line one\nLine two")).toBe('"Line one\nLine two"');
    expect(csvCell(" padded ")).toBe('" padded "');
    expect(csvCell("Café Zoë")).toBe("Café Zoë");
    expect(csvCell("")).toBe("");
    // A minus sign inside a name is harmless.
    expect(csvCell("Wal-Mart")).toBe("Wal-Mart");
  });

  it("writes one CRLF-terminated line per row", () => {
    expect(toCsv({ header: ["Payee", "Amount"], rows: [["=cmd|' /C calc'!A0", "-5.00"], ["Cafe, Uno", "12.00"]] })).toBe(
      "Payee,Amount\r\n'=cmd|' /C calc'!A0,-5.00\r\n\"Cafe, Uno\",12.00\r\n",
    );
  });
});

describe("report csv tables", () => {
  it("lists each group with its share, the total and anything not converted", () => {
    const table = flowCsv(flowReport({ unconverted: [money("120.00", "EUR")] }));
    expect(table.header).toEqual(["Category", "Amount", "Currency", "Share of total (%)"]);
    expect(table.rows).toEqual([
      ["Rent", "1450.00", "USD", "80.6"],
      ["Food", "300.50", "USD", "16.7"],
      ["Uncategorised", "49.50", "USD", "2.8"],
      ["Total", "1800.00", "USD", "100.0"],
      ["Not converted (no exchange rate)", "120.00", "EUR", ""],
    ]);
  });

  it("names the first column after the grouping and keeps months sortable", () => {
    const byPayee = flowCsv(
      flowReport({
        group_by: "payee",
        items: [
          { key: "p-1", name: "=HYPERLINK(evil)", amount: money("10.00") },
          { key: null, name: null, amount: money("-2.50") },
        ],
        total: money("7.50"),
      }),
    );
    expect(byPayee.header[0]).toBe("Payee");
    expect(toCsv(byPayee)).toBe(
      ["Payee,Amount,Currency,Share of total (%)", "'=HYPERLINK(evil),10.00,USD,133.3", "No payee,-2.50,USD,-33.3", "Total,7.50,USD,100.0", ""].join("\r\n"),
    );

    const byMonth = flowCsv(flowReport({ group_by: "month", items: [{ key: "2026-11", name: null, amount: money("0.00") }], total: money("0.00") }));
    expect(byMonth.header[0]).toBe("Month");
    expect(byMonth.rows).toEqual([
      ["2026-11", "0.00", "USD", ""],
      ["Total", "0.00", "USD", ""],
    ]);
  });

  it("writes cash flow per month with totals", () => {
    const table = cashflowCsv(cashflowReport({ unconverted: [money("-80.00", "GBP")] }));
    expect(table.header).toEqual(["Month", "Money in", "Money out", "Net", "Currency"]);
    expect(table.rows).toEqual([
      ["2026-10", "3466.18", "3667.08", "-200.90", "USD"],
      ["2026-11", "3000.00", "2323.59", "676.41", "USD"],
      ["Total", "6466.18", "5990.67", "475.51", "USD"],
      ["Not converted (no exchange rate)", "", "", "-80.00", "GBP"],
    ]);
    expect(toCsv(table)).toContain("2026-10,3466.18,3667.08,-200.90,USD\r\n");
  });

  it("writes the trend comparison and the monthly series", () => {
    const report = trendReport({ unconverted: [money("15.00", "EUR")] });
    const table = trendCsv(report);
    expect(table.header).toEqual([
      "Category",
      "2026-11",
      "2026-10",
      "Change from 2026-10",
      "Change from 2026-10 (%)",
      "2025-11",
      "Change from 2025-11",
      "Change from 2025-11 (%)",
      "Currency",
    ]);
    expect(table.rows[0]).toEqual(["Rent", "1450.00", "1450.00", "0.00", "0.00", "0.00", "1450.00", "", "USD"]);
    expect(table.rows[1]).toEqual(["Food", "350.00", "550.00", "-200.00", "-36.36", "0.00", "350.00", "", "USD"]);
    expect(table.rows[2]).toEqual(["Total", "1800.00", "2000.00", "-200.00", "-10.00", "0.00", "1800.00", "", "USD"]);
    expect(table.rows[3]?.[0]).toBe("Not converted (no exchange rate): 15.00 EUR over the months compared and charted");
    expect(table.rows.every((row) => row.length === table.header.length)).toBe(true);

    expect(seriesCsv(report)).toEqual({
      header: ["Month", "Spending", "Currency"],
      rows: [
        ["2026-09", "1500.00", "USD"],
        ["2026-10", "2000.00", "USD"],
        ["2026-11", "1800.00", "USD"],
      ],
    });
    expect(seriesCsv(trendReport({ flow: "income" })).header[1]).toBe("Income");
    expect(trendCsv(trendReport({ categories: [{ ...report.total, key: CAT_RENT, name: null }] })).rows[0]?.[0]).toBe("Unknown category");
  });

  it("builds a plain file name", () => {
    expect(csvFilename(["spending", "by", "category", "2026-11-01", "to", "2026-11-30"])).toBe("perfin-spending-by-category-2026-11-01-to-2026-11-30.csv");
    expect(csvFilename(["Cash flow", "../etc", ""])).toBe("perfin-cash-flow-etc.csv");
  });
});
