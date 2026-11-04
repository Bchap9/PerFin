// SPDX-License-Identifier: AGPL-3.0-or-later
import { buildScheduleBody, cadenceText, emptyScheduleForm, isBill } from "./scheduleForm";

const form = { ...emptyScheduleForm("2026-11-03"), name: "Rent", accountId: "acc" };

describe("recurring form", () => {
  it("stores a bill as a negative range with min <= max", () => {
    const result = buildScheduleBody({ ...form, low: "100.00", high: "120.00" });
    expect(result).toMatchObject({ ok: true, body: { amount_min: { amount: "-120.00" }, amount_max: { amount: "-100.00" }, active: true } });
    if (result.ok) {
      expect(isBill(result.body as never)).toBe(true);
    }
  });

  it("stores income positive and repeats a single amount", () => {
    expect(buildScheduleBody({ ...form, direction: "income", low: "2000" })).toMatchObject({
      ok: true,
      body: { amount_min: { amount: "2000" }, amount_max: { amount: "2000" } },
    });
  });

  it("flags problems", () => {
    const result = buildScheduleBody({ ...form, name: "", low: "5", high: "3", intervalCount: "0" });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(Object.keys(result.problems).sort()).toEqual(["amount", "interval", "name"]);
    }
  });

  it("words the cadence", () => {
    expect(cadenceText("monthly", 1)).toBe("Every month");
    expect(cadenceText("weekly", 2)).toBe("Every 2 weeks");
  });
});
