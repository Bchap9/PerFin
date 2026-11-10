// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Loan = Schemas["LoanOut"];
export type LoanCreate = Schemas["LoanCreate"];
export type LoanUpdate = Schemas["LoanUpdate"];
export type LoanSchedule = Schemas["app__domain__loans__schemas__ScheduleOut"];
export type ScheduleRow = Schemas["ScheduleRowOut"];
export type ScheduleBasis = LoanSchedule["basis"];
export type LoanPayment = Schemas["LoanPaymentOut"];
export type LoanPaymentCreate = Schemas["LoanPaymentCreate"];
export type RateChange = Schemas["RateChangeOut"];
export type SimulateIn = Schemas["SimulateIn"];
export type Simulation = Schemas["SimulationOut"];

export const loanKeys = {
  list: [...queryRoots.loans, "list"],
  one: (id: string) => [...queryRoots.loans, "one", id],
  schedule: (id: string, basis: string) => [...queryRoots.loans, "schedule", id, basis],
  payments: (id: string) => [...queryRoots.loans, "payments", id],
  rateChanges: (id: string) => [...queryRoots.loans, "rate-changes", id],
} as const;

export function listLoans(): Promise<Loan[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/loans", { params: { query: { limit: 200, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export const getLoan = (id: string) => unwrap(api.GET("/api/v1/loans/{loan_id}", { params: { path: { loan_id: id } } }));
export const createLoan = (body: LoanCreate) => unwrap(api.POST("/api/v1/loans", { body }));
/** Only the terms given change (`escrow_amount` and `lender` may be null to clear them). */
export const updateLoan = (id: string, body: LoanUpdate) =>
  unwrap(api.PATCH("/api/v1/loans/{loan_id}", { params: { path: { loan_id: id } }, body }));
export const deleteLoan = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/loans/{loan_id}", { params: { path: { loan_id: id } } }));
};

export const getSchedule = (id: string, basis: ScheduleBasis) =>
  unwrap(api.GET("/api/v1/loans/{loan_id}/schedule", { params: { path: { loan_id: id }, query: { basis } } }));

export const simulateLoan = (id: string, body: SimulateIn) =>
  unwrap(api.POST("/api/v1/loans/{loan_id}/simulate", { params: { path: { loan_id: id } }, body }));

export const listPayments = async (id: string): Promise<LoanPayment[]> =>
  (await unwrap(api.GET("/api/v1/loans/{loan_id}/payments", { params: { path: { loan_id: id } } }))).items;

export const addPayment = (id: string, body: LoanPaymentCreate) =>
  unwrap(api.POST("/api/v1/loans/{loan_id}/payments", { params: { path: { loan_id: id } }, body }));

export const deletePayment = async (id: string, paymentId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/loans/{loan_id}/payments/{payment_id}", { params: { path: { loan_id: id, payment_id: paymentId } } }),
  );
};

export const listRateChanges = async (id: string): Promise<RateChange[]> =>
  (await unwrap(api.GET("/api/v1/loans/{loan_id}/rate-changes", { params: { path: { loan_id: id } } }))).items;

export const addRateChange = (id: string, body: Schemas["RateChangeCreate"]) =>
  unwrap(api.POST("/api/v1/loans/{loan_id}/rate-changes", { params: { path: { loan_id: id } }, body }));

export const deleteRateChange = async (id: string, changeId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/loans/{loan_id}/rate-changes/{change_id}", { params: { path: { loan_id: id, change_id: changeId } } }),
  );
};
