// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The security form as data: validation and the request bodies for creating
 * a security and for changing one. The patterns are the server's
 * (backend `investments/schemas.py`); the server checks them again. A change
 * sends only the fields that differ, so a locked currency or an untouched
 * symbol is never rewritten by accident.
 */
import { isCurrencyCode } from "../../lib/money";
import type { AssetClass, Security, SecurityCreate, SecurityUpdate } from "./api";

export interface SecurityForm {
  symbol: string;
  name: string;
  assetClass: AssetClass;
  currency: string;
  exchange: string;
  sector: string;
  isin: string;
  cusip: string;
}

export type SecurityProblems = Partial<Record<"symbol" | "name" | "currency" | "isin" | "cusip", string>>;

export const SECURITY_LIMITS = { symbol: 32, name: 200, exchange: 32, sector: 100, isin: 12, cusip: 9 } as const;

const SYMBOL_RE = /^[A-Za-z0-9.^/:_-]{1,32}$/;
const ISIN_RE = /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/;
const CUSIP_RE = /^[0-9A-Z*@#]{9}$/;

/** The form for a new security, or filled from the one being edited. */
export function securityFormOf(security: Security | undefined, defaultCurrency: string): SecurityForm {
  return {
    symbol: security?.symbol ?? "",
    name: security?.name ?? "",
    assetClass: security?.asset_class ?? "equity",
    currency: security?.currency ?? defaultCurrency,
    exchange: security?.exchange ?? "",
    sector: security?.sector ?? "",
    isin: security?.isin ?? "",
    cusip: security?.cusip ?? "",
  };
}

interface Cleaned {
  symbol: string;
  name: string;
  currency: string;
  exchange: string | null;
  sector: string | null;
  isin: string | null;
  cusip: string | null;
}

const optional = (text: string): string | null => (text.trim() === "" ? null : text.trim());

function clean(form: SecurityForm, original: Security | undefined): { cleaned: Cleaned; problems: SecurityProblems } {
  const problems: SecurityProblems = {};
  const typedSymbol = form.symbol.trim();
  // An untouched symbol is kept as stored; a typed one is upper-cased like a new security's.
  const symbol = original !== undefined && typedSymbol === (original.symbol ?? "") ? typedSymbol : typedSymbol.toUpperCase();
  if (symbol === "") {
    // A security a statement named without a symbol may stay without one; a symbol, once set, cannot be removed.
    if (original?.symbol !== null) {
      problems.symbol = "Enter the ticker symbol.";
    }
  } else if (!SYMBOL_RE.test(symbol)) {
    problems.symbol = "Use up to 32 letters, digits and . ^ / : _ - without spaces.";
  }
  const name = form.name.trim();
  if (name === "") {
    problems.name = "Enter a name.";
  }
  const currency = form.currency.trim().toUpperCase();
  if (!isCurrencyCode(currency)) {
    problems.currency = "Enter a three-letter currency code.";
  }
  const isin = optional(form.isin)?.toUpperCase() ?? null;
  if (isin !== null && !ISIN_RE.test(isin)) {
    problems.isin = "An ISIN has 12 characters: two letters, nine letters or digits, then a digit.";
  }
  const cusip = optional(form.cusip)?.toUpperCase() ?? null;
  if (cusip !== null && !CUSIP_RE.test(cusip)) {
    problems.cusip = "A CUSIP has 9 characters.";
  }
  return {
    cleaned: { symbol, name, currency, exchange: optional(form.exchange), sector: optional(form.sector), isin, cusip },
    problems,
  };
}

export type SecurityBody<B> = { ok: true; body: B } | { ok: false; problems: SecurityProblems };

const hasProblems = (problems: SecurityProblems): boolean => Object.keys(problems).length > 0;

export function buildSecurityCreate(form: SecurityForm): SecurityBody<SecurityCreate> {
  const { cleaned, problems } = clean(form, undefined);
  if (hasProblems(problems)) {
    return { ok: false, problems };
  }
  return {
    ok: true,
    body: {
      symbol: cleaned.symbol,
      name: cleaned.name,
      asset_class: form.assetClass,
      currency: cleaned.currency,
      ...(cleaned.exchange === null ? {} : { exchange: cleaned.exchange }),
      ...(cleaned.sector === null ? {} : { sector: cleaned.sector }),
      ...(cleaned.isin === null ? {} : { isin: cleaned.isin }),
      ...(cleaned.cusip === null ? {} : { cusip: cleaned.cusip }),
    },
  };
}

/** The fields that differ from `original` (an empty body when nothing changed). */
export function buildSecurityUpdate(form: SecurityForm, original: Security): SecurityBody<SecurityUpdate> {
  const { cleaned, problems } = clean(form, original);
  if (hasProblems(problems)) {
    return { ok: false, problems };
  }
  const body: SecurityUpdate = {};
  if (cleaned.symbol !== "" && cleaned.symbol !== original.symbol) {
    body.symbol = cleaned.symbol;
  }
  if (cleaned.name !== original.name) {
    body.name = cleaned.name;
  }
  if (form.assetClass !== original.asset_class) {
    body.asset_class = form.assetClass;
  }
  if (cleaned.currency !== original.currency) {
    body.currency = cleaned.currency;
  }
  if (cleaned.exchange !== original.exchange) {
    body.exchange = cleaned.exchange;
  }
  if (cleaned.sector !== original.sector) {
    body.sector = cleaned.sector;
  }
  if (cleaned.isin !== original.isin) {
    body.isin = cleaned.isin;
  }
  if (cleaned.cusip !== original.cusip) {
    body.cusip = cleaned.cusip;
  }
  return { ok: true, body };
}
