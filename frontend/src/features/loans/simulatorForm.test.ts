// SPDX-License-Identifier: AGPL-3.0-or-later
import { buildSimulateBody, emptySimulatorForm } from "./simulatorForm";

describe("simulator form", () => {
  it("builds a body from one-off and recurring extras with decimal strings", () => {
    const form = {
      ...emptySimulatorForm(),
      oneOff: [{ key: "a", on: "2027-01-15", amount: "5000.00" }],
      recurringAmount: "150.50",
      recurringStart: "2027-02-01",
      includeSchedule: true,
    };
    expect(buildSimulateBody(form, "USD")).toEqual({
      ok: true,
      body: {
        one_off: [{ on: "2027-01-15", amount: { amount: "5000.00", currency: "USD" } }],
        recurring: { amount: { amount: "150.50", currency: "USD" }, start: "2027-02-01" },
        include_schedule: true,
      },
    });
  });

  it("asks for at least one extra", () => {
    expect(buildSimulateBody(emptySimulatorForm(), "USD")).toMatchObject({ ok: false, problems: { form: expect.stringContaining("Add a one-off") as string } });
  });

  it("rejects bad amounts, dates and ranges per field", () => {
    const form = {
      ...emptySimulatorForm(),
      oneOff: [
        { key: "a", on: "2027-01-15", amount: "-1" },
        { key: "b", on: "2027-13-01", amount: "10" },
      ],
      recurringAmount: "20",
      recurringStart: "2027-05-01",
      recurringEnd: "2027-04-01",
    };
    const result = buildSimulateBody(form, "USD");
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.problems.rows["a"]).toMatch(/above zero/);
      expect(result.problems.rows["b"]).toMatch(/valid date/);
      expect(result.problems.recurringEnd).toMatch(/before the start/);
    }
  });
});
