// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type TaxYear = Schemas["TaxYearOut"];
export type TaxYearState = Schemas["TaxYearState"];
export type ChecklistItem = Schemas["ChecklistItemOut"];
export type ChecklistStatus = Schemas["ChecklistStatus"];
export type FormData = Schemas["FormDataOut"];
export type TaxSummary = Schemas["TaxSummaryOut"];
export type CategoryMapEntry = Schemas["CategoryMapOut"];
export type TaxExport = Schemas["TaxExportOut"];
export type TaxExportState = Schemas["TaxExportState"];

export const taxKeys = {
  years: [...queryRoots.tax, "years"],
  year: (year: number) => [...queryRoots.tax, "year", year],
  checklist: (year: number) => [...queryRoots.tax, "checklist", year],
  forms: (year: number) => [...queryRoots.tax, "forms", year],
  summary: (year: number) => [...queryRoots.tax, "summary", year],
  map: (jurisdiction: string) => [...queryRoots.tax, "map", jurisdiction],
  export: (year: number, jobId: string) => [...queryRoots.tax, "export", year, jobId],
} as const;

/** Form types the US set knows (docs/API.md, tax notes); the server checks them again. */
export const US_FORMS: readonly string[] = [
  "W-2",
  "1099-INT",
  "1099-DIV",
  "1099-B",
  "1099-MISC",
  "1099-NEC",
  "1099-R",
  "1099-G",
  "1099-SA",
  "1098",
  "1098-E",
  "1098-T",
  "1095-A",
  "1095-B",
  "1095-C",
];

export async function listTaxYears(): Promise<TaxYear[]> {
  return (await unwrap(api.GET("/api/v1/tax-years"))).items;
}

export const getTaxYear = (year: number) => unwrap(api.GET("/api/v1/tax-years/{year}", { params: { path: { year } } }));

export const createTaxYear = (body: Schemas["TaxYearCreate"]) => unwrap(api.POST("/api/v1/tax-years", { body }));

export const updateTaxYear = (year: number, body: Schemas["TaxYearUpdate"]) =>
  unwrap(api.PATCH("/api/v1/tax-years/{year}", { params: { path: { year } }, body }));

export const getChecklist = (year: number) =>
  unwrap(api.GET("/api/v1/tax-years/{year}/checklist", { params: { path: { year } } }));

export const addChecklistItem = (year: number, body: Schemas["ChecklistItemCreate"]) =>
  unwrap(api.POST("/api/v1/tax-years/{year}/checklist", { params: { path: { year } }, body }));

export const updateChecklistItem = (year: number, itemId: string, body: Schemas["ChecklistItemUpdate"]) =>
  unwrap(api.PATCH("/api/v1/tax-years/{year}/checklist/{item_id}", { params: { path: { year, item_id: itemId } }, body }));

export const deleteChecklistItem = async (year: number, itemId: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/tax-years/{year}/checklist/{item_id}", { params: { path: { year, item_id: itemId } } }));
};

export const learnChecklist = (year: number) =>
  unwrap(api.POST("/api/v1/tax-years/{year}/checklist/learn", { params: { path: { year } } }));

export async function listForms(year: number): Promise<FormData[]> {
  return (await unwrap(api.GET("/api/v1/tax-years/{year}/forms", { params: { path: { year } } }))).items;
}

export const getSummary = (year: number) =>
  unwrap(api.GET("/api/v1/tax-years/{year}/summary", { params: { path: { year } } }));

export async function listCategoryMap(jurisdiction: string): Promise<CategoryMapEntry[]> {
  return (await unwrap(api.GET("/api/v1/tax-category-map", { params: { query: { jurisdiction } } }))).items;
}

export const putCategoryMap = (categoryId: string, body: Schemas["CategoryMapIn"]) =>
  unwrap(api.PUT("/api/v1/tax-category-map/{category_id}", { params: { path: { category_id: categoryId } }, body }));

export const deleteCategoryMap = async (categoryId: string, jurisdiction: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/tax-category-map/{category_id}", {
      params: { path: { category_id: categoryId }, query: { jurisdiction } },
    }),
  );
};

/** Needs an owner or admin browser session with a recent step-up. */
export const startTaxExport = (year: number) =>
  unwrap(api.POST("/api/v1/tax-years/{year}/export", { params: { path: { year } } }));

export const getTaxExport = (year: number, jobId: string) =>
  unwrap(api.GET("/api/v1/tax-years/{year}/export/{job_id}", { params: { path: { year, job_id: jobId } } }));

/** The zip's bytes. Works once; needs a recent step-up. */
export async function downloadTaxExport(year: number, jobId: string): Promise<Blob> {
  const data: unknown = await unwrap(
    api.GET("/api/v1/tax-years/{year}/export/{job_id}/download", {
      params: { path: { year, job_id: jobId } },
      parseAs: "blob",
    }),
  );
  return data as Blob;
}

/** The "N expected documents still missing" reminder (same wording as the `tax.reminder` event). */
export function reminderText(missing: number): string | null {
  if (missing <= 0) {
    return null;
  }
  return `${String(missing)} expected ${missing === 1 ? "document" : "documents"} still missing`;
}

export const STATUS_LABEL: Record<ChecklistStatus, string> = {
  expected: "Expected",
  received: "Received",
  not_applicable: "Not applicable",
};

export const STATUS_TONE: Record<ChecklistStatus, "warning" | "success" | "neutral"> = {
  expected: "warning",
  received: "success",
  not_applicable: "neutral",
};

export const YEAR_STATE_LABEL: Record<TaxYearState, string> = { open: "Open", filed: "Filed", closed: "Closed" };

export const EXPORT_LABEL: Record<TaxExportState, string> = {
  pending: "Working",
  ready: "Ready to download",
  downloaded: "Downloaded",
  expired: "Expired",
  failed: "Failed",
};

export const EXPORT_TONE: Record<TaxExportState, "info" | "success" | "neutral" | "warning" | "danger"> = {
  pending: "info",
  ready: "success",
  downloaded: "neutral",
  expired: "warning",
  failed: "danger",
};

/** Open years that still have expected documents, newest first. */
export function openMissing(years: readonly TaxYear[]): { year: number; missing: number }[] {
  return years
    .filter((y) => y.state === "open" && y.checklist.expected > 0)
    .map((y) => ({ year: y.year, missing: y.checklist.expected }))
    .sort((a, b) => b.year - a.year);
}
