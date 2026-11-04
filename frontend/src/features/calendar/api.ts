// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type Schedule = Schemas["app__domain__recurring__schemas__ScheduleOut"];
export type ScheduleCreate = Schemas["ScheduleCreate"];
export type ScheduleUpdate = Schemas["ScheduleUpdate"];
export type Cadence = Schemas["Cadence"];
export type Candidate = Schemas["CandidateOut"];
export type CashflowCalendar = Schemas["CashflowCalendar"];
export type CalendarDay = Schemas["CalendarDay"];
export type DetectResult = Schemas["DetectResult"];

export const CADENCES: readonly Cadence[] = ["daily", "weekly", "monthly", "quarterly", "yearly"];
export const CADENCE_LABELS: Record<Cadence, string> = {
  daily: "day",
  weekly: "week",
  monthly: "month",
  quarterly: "quarter",
  yearly: "year",
};

export const recurringKeys = {
  list: [...queryRoots.recurring, "list"],
  candidates: [...queryRoots.recurring, "candidates"],
  calendar: (days: number) => [...queryRoots.recurring, "calendar", days],
} as const;

export async function listSchedules(): Promise<Schedule[]> {
  return (await unwrap(api.GET("/api/v1/recurring", {}))).items;
}

export const createSchedule = (body: ScheduleCreate) => unwrap(api.POST("/api/v1/recurring", { body }));

export const updateSchedule = (id: string, body: ScheduleUpdate) =>
  unwrap(api.PATCH("/api/v1/recurring/{schedule_id}", { params: { path: { schedule_id: id } }, body }));

export const deleteSchedule = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/recurring/{schedule_id}", { params: { path: { schedule_id: id } } }));
};

export const getCandidates = () => unwrap(api.GET("/api/v1/recurring/candidates", {}));

export const detectSchedules = () => unwrap(api.POST("/api/v1/recurring/detect", { body: {} }));

export const getCalendar = (days: number) => unwrap(api.GET("/api/v1/cashflow/calendar", { params: { query: { days } } }));
