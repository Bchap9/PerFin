// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Money for display, without floating point.
 *
 * The API sends money as `{"amount": "123.45", "currency": "USD"}` with the
 * amount as a decimal string (docs/API.md). This module never turns that
 * string into a JavaScript `number`: amounts are parsed into a scaled
 * `bigint` (`units / 10^scale`), added and compared exactly, rounded with
 * integer arithmetic, and handed to `Intl.NumberFormat` as a string (the
 * ES2023 `StringNumericLiteral` overload formats decimal strings exactly).
 *
 * Arithmetic here is for display (totals in a table footer, a running
 * balance). Authoritative calculations happen on the server.
 */

/** An exact decimal: `units / 10^scale`. */
export interface Decimal {
  readonly units: bigint;
  readonly scale: number;
}

export type RoundingMode = "half-expand" | "half-even" | "trunc";

export interface Money {
  readonly amount: string;
  readonly currency: string;
}

/** Longest decimal string accepted (Numeric(20, 6) needs 22 with sign and point). */
export const MAX_DECIMAL_LENGTH = 64;
const MAX_SCALE = 18;
// Sign, whole digits, optional point and fraction (a trailing bare point is rejected below).
const DECIMAL_RE = /^([+-]?)(\d+)\.?(\d*)$/;
const CURRENCY_RE = /^[A-Z]{3}$/;

export class DecimalFormatError extends Error {
  constructor(input: string) {
    super(`not a decimal string: ${JSON.stringify(input.slice(0, 32))}`);
    this.name = "DecimalFormatError";
  }
}

function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

/** Parse a plain decimal string ("12", "-0.50", "+3.000001"). No exponents, no grouping. */
export function tryParseDecimal(input: string): Decimal | null {
  if (typeof input !== "string" || input.length === 0 || input.length > MAX_DECIMAL_LENGTH) {
    return null;
  }
  const match = DECIMAL_RE.exec(input);
  if (match === null || input.endsWith(".")) {
    return null;
  }
  const [, sign, whole = "0", fraction = ""] = match;
  if (fraction.length > MAX_SCALE) {
    return null;
  }
  const magnitude = BigInt(whole + fraction);
  return { units: sign === "-" ? -magnitude : magnitude, scale: fraction.length };
}

export function parseDecimal(input: string): Decimal {
  const value = tryParseDecimal(input);
  if (value === null) {
    throw new DecimalFormatError(input);
  }
  return value;
}

export function isDecimalString(input: unknown): input is string {
  return typeof input === "string" && tryParseDecimal(input) !== null;
}

function rescale(value: Decimal, scale: number): Decimal {
  if (scale < value.scale) {
    throw new RangeError("rescale cannot drop digits; use roundDecimal");
  }
  return { units: value.units * pow10(scale - value.scale), scale };
}

function align(a: Decimal, b: Decimal): [bigint, bigint, number] {
  const scale = Math.max(a.scale, b.scale);
  return [rescale(a, scale).units, rescale(b, scale).units, scale];
}

/** Canonical string, keeping the scale ("1.50" stays "1.50"; "-0" becomes "0"). */
export function decimalToString(value: Decimal): string {
  const negative = value.units < 0n;
  const digits = (negative ? -value.units : value.units).toString();
  let text: string;
  if (value.scale === 0) {
    text = digits;
  } else {
    const padded = digits.padStart(value.scale + 1, "0");
    text = `${padded.slice(0, -value.scale)}.${padded.slice(-value.scale)}`;
  }
  return negative ? `-${text}` : text;
}

export function addDecimals(a: Decimal, b: Decimal): Decimal {
  const [x, y, scale] = align(a, b);
  return { units: x + y, scale };
}

export function negateDecimal(value: Decimal): Decimal {
  return { units: -value.units, scale: value.scale };
}

export function compareDecimals(a: Decimal, b: Decimal): -1 | 0 | 1 {
  const [x, y] = align(a, b);
  return x < y ? -1 : x > y ? 1 : 0;
}

/** Round to `scale` fraction digits with integer arithmetic only. */
export function roundDecimal(value: Decimal, scale: number, mode: RoundingMode = "half-expand"): Decimal {
  if (!Number.isInteger(scale) || scale < 0 || scale > MAX_SCALE) {
    throw new RangeError("scale must be an integer between 0 and 18");
  }
  if (scale >= value.scale) {
    return rescale(value, scale);
  }
  const divisor = pow10(value.scale - scale);
  const negative = value.units < 0n;
  const magnitude = negative ? -value.units : value.units;
  let quotient = magnitude / divisor;
  const remainder = magnitude % divisor;
  const twice = remainder * 2n;
  if (mode === "half-expand" && twice >= divisor) {
    quotient += 1n;
  } else if (mode === "half-even" && (twice > divisor || (twice === divisor && quotient % 2n === 1n))) {
    quotient += 1n;
  }
  return { units: negative ? -quotient : quotient, scale };
}

// ---- string API -------------------------------------------------------------

export function addAmounts(a: string, b: string): string {
  return decimalToString(addDecimals(parseDecimal(a), parseDecimal(b)));
}

export function subtractAmounts(a: string, b: string): string {
  return decimalToString(addDecimals(parseDecimal(a), negateDecimal(parseDecimal(b))));
}

export function sumAmounts(amounts: Iterable<string>): string {
  let total: Decimal = { units: 0n, scale: 0 };
  for (const amount of amounts) {
    total = addDecimals(total, parseDecimal(amount));
  }
  return decimalToString(total);
}

export function negateAmount(amount: string): string {
  return decimalToString(negateDecimal(parseDecimal(amount)));
}

export function compareAmounts(a: string, b: string): -1 | 0 | 1 {
  return compareDecimals(parseDecimal(a), parseDecimal(b));
}

export function amountSign(amount: string): -1 | 0 | 1 {
  const { units } = parseDecimal(amount);
  return units < 0n ? -1 : units > 0n ? 1 : 0;
}

export function isZeroAmount(amount: string): boolean {
  return amountSign(amount) === 0;
}

export function roundAmount(amount: string, scale: number, mode?: RoundingMode): string {
  return decimalToString(roundDecimal(parseDecimal(amount), scale, mode));
}

/** Sum money values that must share one currency. */
export function sumMoney(values: readonly Money[], currency: string): Money {
  for (const value of values) {
    if (value.currency !== currency) {
      throw new RangeError(`cannot add ${value.currency} to ${currency}`);
    }
  }
  return { amount: sumAmounts(values.map((value) => value.amount)), currency };
}

// ---- formatting -------------------------------------------------------------

export function isCurrencyCode(code: unknown): code is string {
  return typeof code === "string" && CURRENCY_RE.test(code);
}

const fractionDigitsCache = new Map<string, number>();

/** The currency's minor-unit digits per ICU (USD 2, JPY 0, BHD 3); 2 if unknown. */
export function currencyFractionDigits(currency: string): number {
  const cached = fractionDigitsCache.get(currency);
  if (cached !== undefined) {
    return cached;
  }
  let digits = 2;
  if (isCurrencyCode(currency)) {
    try {
      digits =
        new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions()
          .maximumFractionDigits ?? 2;
    } catch {
      digits = 2;
    }
  }
  fractionDigitsCache.set(currency, digits);
  return digits;
}

export interface FormatMoneyOptions {
  locale?: string | readonly string[];
  /** Fraction digits to show; defaults to the currency's minor units. */
  fractionDigits?: number;
  signDisplay?: "auto" | "always" | "exceptZero" | "negative" | "never";
  currencyDisplay?: "symbol" | "narrowSymbol" | "code" | "name";
  /** Accounting style shows negatives in parentheses. */
  accounting?: boolean;
}

function asNumericLiteral(text: string): Intl.StringNumericLiteral {
  // A validated decimal string is a StringNumericLiteral; this is a type
  // assertion only, the value is never converted to a number.
  return text as Intl.StringNumericLiteral;
}

function localeArg(locale: string | readonly string[] | undefined): string | string[] | undefined {
  return locale === undefined ? undefined : typeof locale === "string" ? locale : [...locale];
}

/**
 * Format a decimal amount as currency without float conversion.
 * Returns `null` for a malformed amount so callers can render a placeholder.
 */
export function formatMoney(amount: string, currency: string, options: FormatMoneyOptions = {}): string | null {
  const value = tryParseDecimal(amount);
  if (value === null) {
    return null;
  }
  const digits = options.fractionDigits ?? currencyFractionDigits(currency);
  const rounded = decimalToString(roundDecimal(value, digits));
  if (!isCurrencyCode(currency)) {
    return `${rounded} ${String(currency).slice(0, 8)}`.trim();
  }
  try {
    const formatter = new Intl.NumberFormat(localeArg(options.locale), {
      style: "currency",
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      signDisplay: options.signDisplay ?? "auto",
      currencyDisplay: options.currencyDisplay ?? "symbol",
      currencySign: options.accounting === true ? "accounting" : "standard",
    });
    return formatter.format(asNumericLiteral(rounded));
  } catch {
    return `${rounded} ${currency}`;
  }
}

export interface FormatDecimalOptions {
  locale?: string | readonly string[];
  minimumFractionDigits?: number;
  maximumFractionDigits?: number;
  signDisplay?: "auto" | "always" | "exceptZero" | "negative" | "never";
}

/** Format a plain decimal (quantities, rates) with locale grouping, exactly. */
export function formatDecimal(amount: string, options: FormatDecimalOptions = {}): string | null {
  const value = tryParseDecimal(amount);
  if (value === null) {
    return null;
  }
  const max = options.maximumFractionDigits ?? Math.max(value.scale, options.minimumFractionDigits ?? 0);
  const min = Math.min(options.minimumFractionDigits ?? 0, max);
  const rounded = decimalToString(roundDecimal(value, max));
  try {
    return new Intl.NumberFormat(localeArg(options.locale), {
      minimumFractionDigits: min,
      maximumFractionDigits: max,
      signDisplay: options.signDisplay ?? "auto",
    }).format(asNumericLiteral(rounded));
  } catch {
    return rounded;
  }
}
