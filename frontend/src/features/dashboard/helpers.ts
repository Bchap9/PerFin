// SPDX-License-Identifier: AGPL-3.0-or-later
import { parseIsoDate } from "../../lib/dates";

/** The month of an ISO date by its name alone ("October"), in the browser's language. */
export function monthName(iso: string): string {
  const { year, month } = parseIsoDate(iso);
  return new Intl.DateTimeFormat(undefined, { month: "long", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, 1)));
}
