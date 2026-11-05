// SPDX-License-Identifier: AGPL-3.0-or-later
import { buildTxnBody, emptyTxnForm, totalAmount, type TxnForm } from "./txnForm";

const base = (patch: Partial<TxnForm>): TxnForm => ({ ...emptyTxnForm("2026-11-03"), accountId: "acc-1", securityId: "sec-1", ...patch });

describe("investment transaction form", () => {
  it("computes a buy's cash cost from quantity, price and fees, negative", () => {
    const form = base({ type: "buy", quantity: "10", price: "12.345", fees: "1.00" });
    expect(totalAmount(form)).toBe("124.45");
    const result = buildTxnBody(form);
    expect(result).toMatchObject({ ok: true, body: { type: "buy", quantity: "10", price: "12.345", amount: { amount: "-124.45", currency: "USD" }, fees: { amount: "1.00" } } });
  });

  it("nets fees off a sale's proceeds", () => {
    const result = buildTxnBody(base({ type: "sell", quantity: "2", price: "100", fees: "5" }));
    expect(result).toMatchObject({ ok: true, body: { amount: { amount: "195.00" } } });
  });

  it("uses a typed amount over the computed one and keeps dividends positive", () => {
    expect(buildTxnBody(base({ type: "buy", quantity: "1", price: "10", amount: "9.99" }))).toMatchObject({ ok: true, body: { amount: { amount: "-9.99" } } });
    expect(buildTxnBody(base({ type: "dividend", amount: "4.20" }))).toMatchObject({ ok: true, body: { amount: { amount: "4.20" } } });
  });

  it("needs a ratio for a split and sends a zero amount", () => {
    expect(buildTxnBody(base({ type: "split" }))).toMatchObject({ ok: false, problems: { ratio: expect.any(String) as string } });
    expect(buildTxnBody(base({ type: "split", ratioNew: "2", ratioOld: "1" }))).toMatchObject({
      ok: true,
      body: { amount: { amount: "0" }, ratio: { new: "2", old: "1" } },
    });
  });

  it("reports what is missing", () => {
    const result = buildTxnBody(base({ accountId: "", securityId: "", type: "buy", quantity: "0", tradeDate: "nope" }));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(Object.keys(result.problems).sort()).toEqual(["account", "date", "price", "quantity", "security"]);
    }
  });
});
