// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for the document library, page previews and region data. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type DocumentRow = Schemas["DocumentListItem"];
export type DocumentDetail = Schemas["DocumentDetailOut"];
export type DocumentKind = Schemas["DocumentKind"];
export type DocumentStatus = Schemas["DocumentStatus"];
export type DocumentRegions = Schemas["DocumentRegions"];
export type Region = Schemas["Region"];
export type RegionWord = Schemas["RegionWord"];
export type SnippetPart = Schemas["SnippetPart"];

export interface DocumentFilters {
  q: string;
  kind: DocumentKind | "";
  status: DocumentStatus | "";
  taxYear: string;
}

export const EMPTY_FILTERS: DocumentFilters = { q: "", kind: "", status: "", taxYear: "" };

export const docKeys = {
  list: (filters: DocumentFilters) => [...queryRoots.documents, "library", filters] as const,
  one: (id: string) => [...queryRoots.documents, "detail", id] as const,
  regions: (id: string, page: number) => [...queryRoots.documents, "regions", id, page] as const,
  imported: (id: string) => [...queryRoots.documents, "imported", id] as const,
} as const;

export const KIND_LABELS: Readonly<Record<DocumentKind, string>> = {
  statement: "Statement",
  receipt: "Receipt",
  invoice: "Invoice",
  tax_form: "Tax form",
  payslip: "Payslip",
  insurance: "Insurance",
  other: "Other",
  unknown: "Unknown",
};

export const SOURCE_LABELS: Readonly<Record<DocumentRow["source"], string>> = {
  upload: "Upload",
  dropfolder: "Drop folder",
  email: "Email",
  api: "API",
};

export function isKind(value: string): value is DocumentKind {
  return Object.keys(KIND_LABELS).includes(value);
}

export function parseTaxYear(text: string): number | null {
  const year = Number(text);
  return text.trim() !== "" && Number.isInteger(year) ? year : null;
}

export function listDocuments(filters: DocumentFilters, cursor: string | null): Promise<Schemas["DocumentPage"]> {
  const q = filters.q.trim();
  const year = parseTaxYear(filters.taxYear);
  return unwrap(
    api.GET("/api/v1/documents", {
      params: {
        query: {
          limit: 25,
          ...(q === "" ? {} : { q }),
          ...(filters.kind === "" ? {} : { kind: filters.kind }),
          ...(filters.status === "" ? {} : { status: filters.status }),
          ...(year === null ? {} : { tax_year: year }),
          ...(cursor === null ? {} : { cursor }),
        },
      },
    }),
  );
}

export function getDocumentDetail(id: string): Promise<DocumentDetail> {
  return unwrap(api.GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id } } }));
}

export function getRegions(id: string, page: number): Promise<DocumentRegions> {
  return unwrap(
    api.GET("/api/v1/documents/{document_id}/regions", { params: { path: { document_id: id }, query: { page } } }),
  );
}

/** Same-origin URL of a rendered page (PNG, cookie-authenticated, shown with an img element). */
export function previewUrl(id: string, page: number): string {
  return `/api/v1/documents/${encodeURIComponent(id)}/preview?page=${String(page)}`;
}

/** Same-origin URL of the original file; the server answers with an attachment. */
export function downloadUrl(id: string): string {
  return `/api/v1/documents/${encodeURIComponent(id)}/download`;
}

export function reprocessDocument(id: string): Promise<unknown> {
  return unwrap(api.POST("/api/v1/documents/{document_id}/reprocess", { params: { path: { document_id: id } } }));
}

export type DocumentImported = Schemas["DocumentImportedOut"];

export function getImported(id: string): Promise<DocumentImported> {
  return unwrap(api.GET("/api/v1/documents/{document_id}/imported", { params: { path: { document_id: id } } }));
}

/**
 * Delete a document. With `withData` the transactions, investment
 * transactions, loan payments, balance snapshots and child documents imported
 * from it go too; without it they stay, unlinked from the file.
 */
export async function deleteDocument(id: string, withData: boolean): Promise<void> {
  await unwrap(
    api.DELETE("/api/v1/documents/{document_id}", {
      params: { path: { document_id: id }, query: { with_data: withData } },
    }),
  );
}
