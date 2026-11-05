// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Dates for display.
 *
 * The API uses `YYYY-MM-DD` for calendar dates (posting dates, statement
 * periods) and RFC 3339 UTC timestamps for instants (docs/API.md). A calendar
 * date has no time zone: it is formatted in UTC so "2026-01-31" never shows
 * up as January 30 in a western time zone. Instants are formatted in the
 * viewer's time zone unless one is given.
 */

export interface PlainDate {
  readonly year: number;
  readonly month: number; // 1-12
  readonly day: number; // 1-31
}

const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
// RFC 3339 with a mandatory offset ("Z" or +hh:mm); fractional seconds optional.
const RFC3339_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d{1,9}|)(?:Z|[+-]\d{2}:\d{2})$/i;
const DAY_MS = 86_400_000;

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

export function daysInMonth(year: number, month: number): number {
  const lengths = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return lengths[month - 1] ?? 0;
}

export function tryParseIsoDate(input: string): PlainDate | null {
  if (typeof input !== "string") {
    return null;
  }
  const match = ISO_DATE_RE.exec(input);
  if (match === null) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
    return null;
  }
  return { year, month, day };
}

export function isIsoDate(input: unknown): input is string {
  return typeof input === "string" && tryParseIsoDate(input) !== null;
}

export function parseIsoDate(input: string): PlainDate {
  const value = tryParseIsoDate(input);
  if (value === null) {
    throw new RangeError(`not a YYYY-MM-DD date: ${JSON.stringify(input.slice(0, 32))}`);
  }
  return value;
}

export function plainDateToIso(date: PlainDate): string {
  const y = String(date.year).padStart(4, "0");
  const m = String(date.month).padStart(2, "0");
  const d = String(date.day).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function toUtcMs(date: PlainDate): number {
  const ms = Date.UTC(date.year, date.month - 1, date.day);
  // Date.UTC maps years 0-99 to 1900-1999; correct for them.
  return date.year < 100 ? new Date(ms).setUTCFullYear(date.year) : ms;
}

function fromUtcMs(ms: number): PlainDate {
  const d = new Date(ms);
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

export function addDays(iso: string, days: number): string {
  if (!Number.isInteger(days)) {
    throw new RangeError("days must be an integer");
  }
  return plainDateToIso(fromUtcMs(toUtcMs(parseIsoDate(iso)) + days * DAY_MS));
}

/** Add months, clamping the day ("2026-01-31" + 1 month = "2026-02-28"). */
export function addMonths(iso: string, months: number): string {
  if (!Number.isInteger(months)) {
    throw new RangeError("months must be an integer");
  }
  const date = parseIsoDate(iso);
  const index = date.year * 12 + (date.month - 1) + months;
  const year = Math.floor(index / 12);
  const month = (index % 12) + 1;
  return plainDateToIso({ year, month, day: Math.min(date.day, daysInMonth(year, month)) });
}

export function startOfMonth(iso: string): string {
  const date = parseIsoDate(iso);
  return plainDateToIso({ ...date, day: 1 });
}

export function endOfMonth(iso: string): string {
  const date = parseIsoDate(iso);
  return plainDateToIso({ ...date, day: daysInMonth(date.year, date.month) });
}

/** ISO dates compare correctly as strings once validated. */
export function compareIsoDates(a: string, b: string): -1 | 0 | 1 {
  const x = plainDateToIso(parseIsoDate(a));
  const y = plainDateToIso(parseIsoDate(b));
  return x < y ? -1 : x > y ? 1 : 0;
}

export function daysBetween(from: string, to: string): number {
  return Math.round((toUtcMs(parseIsoDate(to)) - toUtcMs(parseIsoDate(from))) / DAY_MS);
}

/** The calendar date of `instant` in `timeZone` (default: the viewer's). */
export function toIsoDate(instant: Date, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    calendar: "gregory",
    numberingSystem: "latn",
  }).formatToParts(instant);
  const get = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(parts.find((part) => part.type === type)?.value ?? Number.NaN);
  return plainDateToIso({ year: get("year"), month: get("month"), day: get("day") });
}

export function todayIsoDate(timeZone?: string, now: Date = new Date()): string {
  return toIsoDate(now, timeZone);
}

export function isRfc3339(input: unknown): input is string {
  return typeof input === "string" && RFC3339_RE.test(input) && !Number.isNaN(Date.parse(input));
}

export function parseTimestamp(input: string): Date {
  if (!isRfc3339(input)) {
    throw new RangeError(`not an RFC 3339 timestamp: ${JSON.stringify(String(input).slice(0, 40))}`);
  }
  return new Date(input);
}

type Locale = string | readonly string[] | undefined;

function localeArg(locale: Locale): string | string[] | undefined {
  return locale === undefined ? undefined : typeof locale === "string" ? locale : [...locale];
}

export type DateStyle = "full" | "long" | "medium" | "short";

/** Format a `YYYY-MM-DD` date; `null` if malformed. Never shifted by time zone. */
export function formatDate(iso: string, options: { locale?: Locale; style?: DateStyle } = {}): string | null {
  const date = tryParseIsoDate(iso);
  if (date === null) {
    return null;
  }
  return new Intl.DateTimeFormat(localeArg(options.locale), {
    dateStyle: options.style ?? "medium",
    timeZone: "UTC",
  }).format(toUtcMs(date));
}

/** "Jan 2026"-style label for a month containing `iso`. */
export function formatMonth(iso: string, options: { locale?: Locale; style?: "long" | "short" } = {}): string | null {
  const date = tryParseIsoDate(iso);
  if (date === null) {
    return null;
  }
  return new Intl.DateTimeFormat(localeArg(options.locale), {
    year: "numeric",
    month: options.style ?? "short",
    timeZone: "UTC",
  }).format(toUtcMs(date));
}

/** Format an RFC 3339 instant; `null` if malformed. */
export function formatDateTime(
  timestamp: string,
  options: { locale?: Locale; timeZone?: string; dateStyle?: DateStyle; timeStyle?: DateStyle } = {},
): string | null {
  if (!isRfc3339(timestamp)) {
    return null;
  }
  try {
    return new Intl.DateTimeFormat(localeArg(options.locale), {
      dateStyle: options.dateStyle ?? "medium",
      timeStyle: options.timeStyle ?? "short",
      timeZone: options.timeZone,
    }).format(new Date(timestamp));
  } catch {
    return null;
  }
}

const RELATIVE_UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * DAY_MS],
  ["month", 30 * DAY_MS],
  ["week", 7 * DAY_MS],
  ["day", DAY_MS],
  ["hour", 3_600_000],
  ["minute", 60_000],
  ["second", 1_000],
];

/** "3 minutes ago" / "in 2 days"; `null` if malformed. */
export function formatRelativeTime(
  timestamp: string,
  options: { locale?: Locale; now?: Date } = {},
): string | null {
  if (!isRfc3339(timestamp)) {
    return null;
  }
  const delta = Date.parse(timestamp) - (options.now ?? new Date()).getTime();
  const formatter = new Intl.RelativeTimeFormat(localeArg(options.locale), { numeric: "auto" });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(delta) >= size || unit === "second") {
      return formatter.format(Math.trunc(delta / size), unit);
    }
  }
  return null;
}
