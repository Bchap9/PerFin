// SPDX-License-Identifier: AGPL-3.0-or-later
/** Checking and wording exchange rates (no React, no network). */
import { isApiError } from "../../lib/api/client";
import { compareIsoDates, formatDate, isIsoDate } from "../../lib/dates";
import { formatDecimal, isCurrencyCode } from "../../lib/money";
import type { FxPairFilter, FxRateSet } from "./fxApi";

const DIGITS_RE = /^[0-9]{1,12}$/;
const ZERO_RE = /^[0.]+$/;

/** The server's rate format: at most 12 digits before and 12 after the point. */
function isRate(text: string): boolean {
  const parts = text.split(".");
  return parts.length <= 2 && parts.every((part) => DIGITS_RE.test(part));
}
/** The server refuses dates before this (backend `MIN_BUSINESS_DATE`). */
const EARLIEST = "1900-01-01";

export interface RateForm {
  base: string;
  quote: string;
  date: string;
  rate: string;
}

export function cleanCurrency(text: string): string {
  return text.trim().toUpperCase();
}

/** "1,08" as typed in many countries is "1.08"; anything else is left for the check. */
export function cleanRate(text: string): string {
  const trimmed = text.trim();
  return /^[0-9]+,[0-9]+$/.test(trimmed) ? trimmed.replace(",", ".") : trimmed;
}

/** `FxRateSet` with the rate as the decimal string it is always sent as. */
export type RateBody = Omit<FxRateSet, "rate"> & { rate: string };

export interface BuiltRate {
  body: RateBody | null;
  errors: Record<string, string>;
}

const CODE_HINT = "Use a three-letter currency code, such as EUR.";

/** The request body for the form, or what is wrong with it by field. */
export function buildRate(form: RateForm, today: string): BuiltRate {
  const errors: Record<string, string> = {};
  const base = cleanCurrency(form.base);
  const quote = cleanCurrency(form.quote);
  const rate = cleanRate(form.rate);
  if (!isCurrencyCode(base)) {
    errors["base"] = CODE_HINT;
  }
  if (!isCurrencyCode(quote)) {
    errors["quote"] = CODE_HINT;
  } else if (quote === base) {
    errors["quote"] = "Choose two different currencies.";
  }
  if (!isIsoDate(form.date)) {
    errors["rate_date"] = "Choose the day the rate applies from.";
  } else if (compareIsoDates(form.date, today) > 0) {
    errors["rate_date"] = "A rate cannot be dated in the future.";
  } else if (compareIsoDates(form.date, EARLIEST) < 0) {
    errors["rate_date"] = "Choose a date in 1900 or later.";
  }
  if (rate === "") {
    errors["rate"] = "Enter the rate.";
  } else if (!isRate(rate)) {
    errors["rate"] = "Use a plain number such as 1.0812, with at most 12 digits before and 12 after the point.";
  } else if (ZERO_RE.test(rate)) {
    errors["rate"] = "The rate must be above zero.";
  }
  if (Object.keys(errors).length > 0) {
    return { body: null, errors };
  }
  return { body: { base, quote, rate_date: form.date, rate }, errors };
}

/** "1 EUR = 1.08 USD": the whole meaning of a rate in one line. */
export function rateInWords(rate: { base: string; quote: string; rate: string }): string {
  return `1 ${rate.base} = ${formatDecimal(rate.rate) ?? rate.rate} ${rate.quote}`;
}

/**
 * The same rate seen from the other side, rounded for reading only ("1 USD is
 * about 0.9259 EUR"); it helps to notice a rate entered the wrong way round.
 */
export function inverseInWords(rate: { base: string; quote: string; rate: string }): string | null {
  const value = Number(rate.rate);
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  const inverse = Number((1 / value).toPrecision(4));
  return `1 ${rate.quote} is about ${formatDecimal(String(inverse)) ?? String(inverse)} ${rate.base}`;
}

export function dayLabel(iso: string): string {
  return formatDate(iso) ?? iso;
}

export type BuiltFilter = { ok: true; pair: FxPairFilter } | { ok: false; errors: Record<string, string> };

/** The filter to apply; either side may be left empty. */
export function buildFilter(draft: FxPairFilter): BuiltFilter {
  const base = cleanCurrency(draft.base);
  const quote = cleanCurrency(draft.quote);
  const errors: Record<string, string> = {};
  if (base !== "" && !isCurrencyCode(base)) {
    errors["base"] = CODE_HINT;
  }
  if (quote !== "" && !isCurrencyCode(quote)) {
    errors["quote"] = CODE_HINT;
  }
  return Object.keys(errors).length > 0 ? { ok: false, errors } : { ok: true, pair: { base, quote } };
}

/** "EUR and GBP", "EUR, GBP and JPY". */
export function listInWords(items: readonly string[]): string {
  if (items.length <= 1) {
    return items[0] ?? "";
  }
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1) ?? ""}`;
}

const RATE_FIELDS = ["base", "quote", "rate_date", "rate"] as const;

/** "Value error, must be above zero" as a sentence: "Must be above zero." */
function tidy(message: string): string {
  const text = message.replace(/^Value error, /, "");
  const sentence = `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
  return sentence.endsWith(".") ? sentence : `${sentence}.`;
}

/** The server's validation messages for the four fields of a rate, by field. */
export function serverFieldErrors(error: unknown): Record<string, string> {
  if (!isApiError(error)) {
    return {};
  }
  const found = error.fieldErrors();
  const out: Record<string, string> = {};
  for (const field of RATE_FIELDS) {
    // eslint-disable-next-line security/detect-object-injection -- field is one of the four fixed names
    const message = found[field];
    if (message !== undefined) {
      // eslint-disable-next-line security/detect-object-injection -- field is one of the four fixed names
      out[field] = tidy(message);
    }
  }
  return out;
}
