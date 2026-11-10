// SPDX-License-Identifier: AGPL-3.0-or-later
import { tx } from "./fixtures";
import { amountLock, createBody, parsePositiveAmount, signedAmount, unsignedAmount, updateBody, type TransactionForm } from "./form";

const form = (extra: Partial<TransactionForm> = {}): TransactionForm => ({
  accountId: "a-1",
  date: "2026-11-01",
  direction: "out",
  amount: "42.10",
  payeeId: "p-1",
  description: "CORNER MARKET 12",
  categoryId: "c-1",
  memo: "",
  status: "cleared",
  ...extra,
});

describe("the amount field", () => {
  it("accepts a plain positive amount and tidies it", () => {
    expect(parsePositiveAmount("12.50", "USD")).toEqual({ ok: true, amount: "12.50" });
    expect(parsePositiveAmount(" 1,234.5 ", "USD")).toEqual({ ok: true, amount: "1234.5" });
    expect(parsePositiveAmount(".5", "USD")).toEqual({ ok: true, amount: "0.5" });
    expect(parsePositiveAmount("007", "USD")).toEqual({ ok: true, amount: "7" });
    expect(parsePositiveAmount("1500", "JPY")).toEqual({ ok: true, amount: "1500" });
  });

  it("asks for the direction instead of a sign", () => {
    expect(parsePositiveAmount("-12.50", "USD")).toEqual({ ok: false, error: "Enter the amount without a sign, then choose money out or money in." });
    expect(parsePositiveAmount("+3", "USD")).toMatchObject({ ok: false });
  });

  it("refuses nothing, zero, words and more decimals than the currency has", () => {
    expect(parsePositiveAmount("", "USD")).toEqual({ ok: false, error: "Enter an amount such as 12.50." });
    expect(parsePositiveAmount("twelve", "USD")).toMatchObject({ ok: false });
    expect(parsePositiveAmount("1e3", "USD")).toMatchObject({ ok: false });
    expect(parsePositiveAmount("0.00", "USD")).toEqual({ ok: false, error: "Enter an amount above zero." });
    expect(parsePositiveAmount("1.005", "USD")).toEqual({ ok: false, error: "Use at most 2 decimal places for USD." });
    expect(parsePositiveAmount("10.5", "JPY")).toEqual({ ok: false, error: "JPY amounts have no decimal places." });
  });

  it("turns the direction into the stored sign and back, exactly", () => {
    expect(signedAmount("out", "42.10")).toBe("-42.10");
    expect(signedAmount("in", "42.10")).toBe("42.10");
    expect(unsignedAmount("-42.10")).toEqual({ direction: "out", amount: "42.10" });
    expect(unsignedAmount("1234567890123.45")).toEqual({ direction: "in", amount: "1234567890123.45" });
    expect(unsignedAmount("not money")).toEqual({ direction: "out", amount: "" });
  });
});

describe("request bodies", () => {
  it("builds a create body with only the fields that were filled in", () => {
    expect(createBody(form({ payeeId: "", description: "  Farm stand ", categoryId: "", memo: " cash " }), "8", "USD")).toEqual({
      account_id: "a-1",
      posted_date: "2026-11-01",
      amount: { amount: "-8", currency: "USD" },
      status: "cleared",
      raw_payee: "Farm stand",
      memo: "cash",
    });
    expect(createBody(form({ direction: "in", description: "", status: "pending" }), "100.00", "USD")).toEqual({
      account_id: "a-1",
      posted_date: "2026-11-01",
      amount: { amount: "100.00", currency: "USD" },
      status: "pending",
      payee_id: "p-1",
      category_id: "c-1",
    });
  });

  it("patches nothing when nothing changed", () => {
    expect(updateBody(tx("x-1"), form(), "42.10")).toEqual({});
    // "42.1" out is the same money as "-42.10".
    expect(updateBody(tx("x-1"), form({ amount: "42.1" }), "42.1")).toEqual({});
    expect(updateBody(tx("x-1"), form(), null)).toEqual({});
  });

  it("patches only what changed and clears with null", () => {
    expect(
      updateBody(tx("x-1", { memo: "old" }), form({ date: "2026-11-02", direction: "in", payeeId: "", description: "", categoryId: "c-2", memo: "", status: "reconciled" }), "42.10"),
    ).toEqual({
      posted_date: "2026-11-02",
      amount: { amount: "42.10", currency: "USD" },
      payee_id: null,
      raw_payee: null,
      category_id: "c-2",
      memo: null,
      status: "reconciled",
    });
  });

  it("never sends a category for a split transaction", () => {
    const split = tx("x-1", {
      category_id: null,
      splits: [
        { id: "s-1", amount: { amount: "-40.00", currency: "USD" }, category_id: "c-1", memo: null, tax_flag: false },
        { id: "s-2", amount: { amount: "-2.10", currency: "USD" }, category_id: "c-2", memo: null, tax_flag: false },
      ],
    });
    expect(updateBody(split, form({ categoryId: "c-2", memo: "note" }), null)).toEqual({ memo: "note" });
  });
});

describe("amountLock", () => {
  it("explains why a split transaction or a transfer leg keeps its amount", () => {
    expect(amountLock(tx("x-1"))).toBeNull();
    expect(amountLock(tx("x-1", { transfer: { group_id: "g-1", counterpart_id: "x-2" } }))).toMatch(/transfer/);
    expect(
      amountLock(tx("x-1", { splits: [{ id: "s-1", amount: { amount: "-42.10", currency: "USD" }, category_id: null, memo: null, tax_flag: false }] })),
    ).toMatch(/split/);
  });
});
