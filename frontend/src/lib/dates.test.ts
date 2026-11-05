// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  addDays,
  addMonths,
  compareIsoDates,
  daysBetween,
  endOfMonth,
  formatDate,
  formatDateTime,
  formatMonth,
  formatRelativeTime,
  isIsoDate,
  isRfc3339,
  parseIsoDate,
  parseTimestamp,
  startOfMonth,
  toIsoDate,
  todayIsoDate,
} from "./dates";

describe("ISO dates", () => {
  it("validates real calendar dates only", () => {
    expect(isIsoDate("2024-02-29")).toBe(true);
    expect(isIsoDate("2023-02-29")).toBe(false);
    expect(isIsoDate("1900-02-29")).toBe(false);
    expect(isIsoDate("2000-02-29")).toBe(true);
    expect(isIsoDate("2026-13-01")).toBe(false);
    expect(isIsoDate("2026-1-01")).toBe(false);
    expect(isIsoDate("2026-01-01T00:00:00Z")).toBe(false);
    expect(isIsoDate(20260101)).toBe(false);
    expect(parseIsoDate("2026-11-03")).toEqual({ year: 2026, month: 11, day: 3 });
    expect(() => parseIsoDate("nope")).toThrow(RangeError);
  });

  it("does calendar arithmetic without time-zone drift", () => {
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2024-03-01", -1)).toBe("2024-02-29");
    expect(addDays("2026-03-08", 1)).toBe("2026-03-09"); // US DST change
    expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
    expect(addMonths("2024-01-31", 1)).toBe("2024-02-29");
    expect(addMonths("2026-01-15", -1)).toBe("2025-12-15");
    expect(addMonths("2026-11-30", 14)).toBe("2028-01-30");
    expect(startOfMonth("2026-11-03")).toBe("2026-11-01");
    expect(endOfMonth("2026-02-10")).toBe("2026-02-28");
    expect(compareIsoDates("2026-01-02", "2025-12-31")).toBe(1);
    expect(compareIsoDates("2026-01-02", "2026-01-02")).toBe(0);
    expect(daysBetween("2026-01-01", "2026-03-01")).toBe(59);
    expect(() => addDays("2026-01-01", 0.5)).toThrow(RangeError);
  });

  it("formats a date as the same calendar day in every time zone", () => {
    expect(formatDate("2026-01-31", { locale: "en-US" })).toBe("Jan 31, 2026");
    expect(formatDate("2026-01-01", { locale: "en-US", style: "long" })).toBe("January 1, 2026");
    expect(formatDate("2026-01-31", { locale: "de-DE", style: "short" })).toBe("31.01.26");
    expect(formatDate("2026-02-30")).toBeNull();
    expect(formatMonth("2026-11-03", { locale: "en-US", style: "long" })).toBe("November 2026");
  });

  it("finds the calendar date of an instant in a time zone", () => {
    const instant = new Date("2026-11-03T02:30:00Z");
    expect(toIsoDate(instant, "UTC")).toBe("2026-11-03");
    expect(toIsoDate(instant, "America/Los_Angeles")).toBe("2026-11-02");
    expect(toIsoDate(instant, "Asia/Tokyo")).toBe("2026-11-03");
    expect(todayIsoDate("Pacific/Auckland", instant)).toBe("2026-11-03");
  });
});

describe("timestamps", () => {
  it("accepts RFC 3339 with an offset only", () => {
    expect(isRfc3339("2026-11-03T12:00:00Z")).toBe(true);
    expect(isRfc3339("2026-11-03T12:00:00.123456+02:00")).toBe(true);
    expect(isRfc3339("2026-11-03T12:00:00")).toBe(false);
    expect(isRfc3339("2026-11-03")).toBe(false);
    expect(isRfc3339("2026-11-03T12:00:00.Z")).toBe(false);
    expect(parseTimestamp("2026-11-03T12:00:00Z").toISOString()).toBe("2026-11-03T12:00:00.000Z");
    expect(() => parseTimestamp("yesterday")).toThrow(RangeError);
  });

  it("formats instants in the requested zone", () => {
    expect(formatDateTime("2026-11-03T17:05:00Z", { locale: "en-US", timeZone: "UTC" })).toBe("Nov 3, 2026, 5:05 PM");
    expect(formatDateTime("2026-11-03T17:05:00Z", { locale: "en-US", timeZone: "America/New_York" })).toBe(
      "Nov 3, 2026, 12:05 PM",
    );
    expect(formatDateTime("not a time")).toBeNull();
    expect(formatDateTime("2026-11-03T17:05:00Z", { timeZone: "Not/AZone" })).toBeNull();
  });

  it("formats relative times", () => {
    const now = new Date("2026-11-03T12:00:00Z");
    expect(formatRelativeTime("2026-11-03T11:57:00Z", { locale: "en-US", now })).toBe("3 minutes ago");
    expect(formatRelativeTime("2026-11-05T12:00:00Z", { locale: "en-US", now })).toBe("in 2 days");
    expect(formatRelativeTime("2026-11-02T12:00:00Z", { locale: "en-US", now })).toBe("yesterday");
    expect(formatRelativeTime("2026-11-03T12:00:00Z", { locale: "en-US", now })).toBe("now");
    expect(formatRelativeTime("garbage", { now })).toBeNull();
  });
});
