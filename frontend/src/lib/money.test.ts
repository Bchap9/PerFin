// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  addAmounts,
  amountSign,
  compareAmounts,
  currencyFractionDigits,
  decimalToString,
  formatDecimal,
  formatMoney,
  isDecimalString,
  negateAmount,
  parseDecimal,
  roundAmount,
  subtractAmounts,
  sumAmounts,
  sumMoney,
  tryParseDecimal,
} from "./money";

describe("parseDecimal", () => {
  it("parses plain decimal strings into scaled bigints", () => {
    expect(parseDecimal("123.45")).toEqual({ units: 12345n, scale: 2 });
    expect(parseDecimal("-0.000001")).toEqual({ units: -1n, scale: 6 });
    expect(parseDecimal("+7")).toEqual({ units: 7n, scale: 0 });
    expect(parseDecimal("00012.50")).toEqual({ units: 1250n, scale: 2 });
  });

  it.each(["", "1e3", "1,000.00", " 1", "1.", ".5", "--1", "NaN", "Infinity", "0x10", "1.2.3", "１２"])(
    "rejects %j",
    (input) => {
      expect(tryParseDecimal(input)).toBeNull();
      expect(isDecimalString(input)).toBe(false);
      expect(() => parseDecimal(input)).toThrow(/not a decimal string/);
    },
  );

  it("rejects absurdly long input before building a bigint", () => {
    expect(tryParseDecimal("9".repeat(65))).toBeNull();
    expect(tryParseDecimal(`1.${"0".repeat(19)}`)).toBeNull();
  });

  it("round-trips through decimalToString and normalises negative zero", () => {
    expect(decimalToString(parseDecimal("-12.340"))).toBe("-12.340");
    expect(decimalToString(parseDecimal("0.05"))).toBe("0.05");
    expect(decimalToString(parseDecimal("-0.00"))).toBe("0.00");
  });
});

describe("decimal arithmetic", () => {
  it("adds exactly where floats do not", () => {
    expect(addAmounts("0.1", "0.2")).toBe("0.3");
    expect(0.1 + 0.2).not.toBe(0.3); // the reason this module exists
    expect(addAmounts("1.005", "-0.005")).toBe("1.000");
    expect(subtractAmounts("10", "0.01")).toBe("9.99");
  });

  it("handles values beyond Number.MAX_SAFE_INTEGER", () => {
    expect(addAmounts("99999999999999.999999", "0.000001")).toBe("100000000000000.000000");
    expect(sumAmounts(["90071992547409.93", "90071992547409.93"])).toBe("180143985094819.86");
  });

  it("sums, negates, compares and signs", () => {
    expect(sumAmounts([])).toBe("0");
    expect(sumAmounts(["1.10", "2.205", "-0.305"])).toBe("3.000");
    expect(negateAmount("5.00")).toBe("-5.00");
    expect(negateAmount("-5")).toBe("5");
    expect(compareAmounts("1.50", "1.5")).toBe(0);
    expect(compareAmounts("-2", "1")).toBe(-1);
    expect(compareAmounts("10.01", "10.001")).toBe(1);
    expect(amountSign("-0.000")).toBe(0);
    expect(amountSign("-0.01")).toBe(-1);
  });

  it("rounds with integer arithmetic", () => {
    expect(roundAmount("2.345", 2)).toBe("2.35");
    expect(roundAmount("-2.345", 2)).toBe("-2.35");
    expect(roundAmount("2.344999", 2)).toBe("2.34");
    expect(roundAmount("2.345", 2, "half-even")).toBe("2.34");
    expect(roundAmount("2.355", 2, "half-even")).toBe("2.36");
    expect(roundAmount("2.349", 2, "trunc")).toBe("2.34");
    expect(roundAmount("7", 2)).toBe("7.00");
    expect(() => roundAmount("1", -1)).toThrow(RangeError);
  });

  it("refuses to sum mixed currencies", () => {
    expect(sumMoney([{ amount: "1.00", currency: "USD" }, { amount: "2.50", currency: "USD" }], "USD")).toEqual({
      amount: "3.50",
      currency: "USD",
    });
    expect(() => sumMoney([{ amount: "1", currency: "EUR" }], "USD")).toThrow(/cannot add EUR to USD/);
  });
});

describe("formatMoney", () => {
  it("formats with the currency's minor units", () => {
    expect(formatMoney("1234.5", "USD", { locale: "en-US" })).toBe("$1,234.50");
    expect(formatMoney("-1234.5", "USD", { locale: "en-US" })).toBe("-$1,234.50");
    expect(formatMoney("1234.5", "JPY", { locale: "en-US" })).toBe("¥1,235");
    expect(formatMoney("1.2345", "BHD", { locale: "en-US", currencyDisplay: "code" })).toMatch(/^BHD\s1\.235$/);
    expect(currencyFractionDigits("JPY")).toBe(0);
    expect(currencyFractionDigits("USD")).toBe(2);
  });

  it("keeps every digit of large amounts (no float conversion)", () => {
    expect(formatMoney("12345678901234567.89", "USD", { locale: "en-US" })).toBe("$12,345,678,901,234,567.89");
    expect(formatMoney("0.105", "USD", { locale: "en-US" })).toBe("$0.11");
    expect(formatMoney("1.005", "USD", { locale: "en-US" })).toBe("$1.01");
  });

  it("respects locale, sign display and accounting style", () => {
    expect(formatMoney("1234.5", "EUR", { locale: "de-DE" })).toBe("1.234,50 €");
    expect(formatMoney("5", "USD", { locale: "en-US", signDisplay: "always" })).toBe("+$5.00");
    expect(formatMoney("-5", "USD", { locale: "en-US", accounting: true })).toBe("($5.00)");
    expect(formatMoney("12", "USD", { locale: "en-US", fractionDigits: 0 })).toBe("$12");
  });

  it("returns null for malformed amounts and degrades for unknown currencies", () => {
    expect(formatMoney("12.3.4", "USD")).toBeNull();
    expect(formatMoney("1e9", "USD")).toBeNull();
    expect(formatMoney("12.5", "usd")).toBe("12.50 usd");
  });
});

describe("formatDecimal", () => {
  it("formats quantities exactly with grouping", () => {
    expect(formatDecimal("1234.5678", { locale: "en-US" })).toBe("1,234.5678");
    expect(formatDecimal("1234.5678", { locale: "en-US", maximumFractionDigits: 2 })).toBe("1,234.57");
    expect(formatDecimal("3", { locale: "en-US", minimumFractionDigits: 2 })).toBe("3.00");
    expect(formatDecimal("x")).toBeNull();
  });
});
