// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for document upload, import preview and column mappings. */
import {
  api,
  ApiError,
  networkError,
  problemFromBody,
  unwrap,
} from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { CSRF_HEADER, csrfStore } from "../../lib/csrf";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type DocumentRow = Schemas["DocumentListItem"];
export type DocumentStatus = Schemas["DocumentStatus"];
export type DocumentReceived = Schemas["DocumentReceivedOut"];
export type ImportPreview = Schemas["ImportPreview"];
export type MappingSpec = Schemas["MappingSpec"];
export type Mapping = Schemas["MappingOut"];

export const importKeys = {
  documents: [...queryRoots.documents, "recent"],
  document: (id: string) => [...queryRoots.documents, "one", id],
  preview: (id: string) => [...queryRoots.documents, "preview", id],
  mappings: [...queryRoots.documents, "mappings"],
} as const;

export function listRecentDocuments(limit = 25): Promise<DocumentRow[]> {
  return unwrap(
    api.GET("/api/v1/documents", { params: { query: { limit } } }),
  ).then((page) => page.items);
}

export function getDocument(id: string): Promise<Schemas["DocumentDetailOut"]> {
  return unwrap(
    api.GET("/api/v1/documents/{document_id}", {
      params: { path: { document_id: id } },
    }),
  );
}

export function getPreview(id: string): Promise<ImportPreview> {
  return unwrap(
    api.GET("/api/v1/imports/{document_id}/preview", {
      params: { path: { document_id: id }, query: { limit: 50 } },
    }),
  );
}

export function listMappings(): Promise<Mapping[]> {
  return unwrap(
    api.GET("/api/v1/imports/mappings", { params: { query: { limit: 200 } } }),
  ).then((page) => page.items);
}

export function createMapping(
  body: Schemas["MappingCreate"],
): Promise<Mapping> {
  return unwrap(api.POST("/api/v1/imports/mappings", { body }));
}

export function applyMapping(
  documentId: string,
  body: Schemas["ApplyMapping"],
): Promise<Schemas["ImportAccepted"]> {
  return unwrap(
    api.POST("/api/v1/imports/{document_id}/apply-mapping", {
      params: { path: { document_id: documentId } },
      body,
    }),
  );
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

export interface UploadHandle {
  promise: Promise<DocumentReceived>;
  abort: () => void;
}

export interface UploadOptions {
  accountId?: string | null;
  onProgress?: (fraction: number) => void;
  /** Injectable for tests. */
  createRequest?: () => XMLHttpRequest;
}

/**
 * Upload one file with progress (`fetch` cannot report upload progress, so
 * this uses XHR with the same CSRF header and same-origin cookies).
 */
export function uploadDocument(
  file: File,
  options: UploadOptions = {},
): UploadHandle {
  const xhr = (options.createRequest ?? (() => new XMLHttpRequest()))();
  const promise = (async (): Promise<DocumentReceived> => {
    const token = await csrfStore.ensure();
    return new Promise<DocumentReceived>((resolve, reject) => {
      const query = options.accountId
        ? `?account_id=${encodeURIComponent(options.accountId)}`
        : "";
      xhr.open("POST", `/api/v1/documents${query}`);
      xhr.withCredentials = true;
      xhr.setRequestHeader("Accept", "application/json");
      if (typeof token === "string") {
        xhr.setRequestHeader(CSRF_HEADER, token);
      }
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          options.onProgress?.(event.loaded / event.total);
        }
      };
      xhr.onerror = () => {
        reject(networkError(new Error("upload failed")));
      };
      xhr.onabort = () => {
        reject(new DOMException("Upload cancelled", "AbortError"));
      };
      xhr.onload = () => {
        const body = parseJson(xhr.responseText);
        if (
          xhr.status >= 200 &&
          xhr.status < 300 &&
          typeof body === "object" &&
          body !== null
        ) {
          resolve(body as DocumentReceived);
        } else {
          reject(problemFromBody(xhr.status, body, xhr.statusText));
        }
      };
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  })();
  return {
    promise,
    abort: () => {
      xhr.abort();
    },
  };
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export { ApiError };

/** Statuses after which the pipeline needs nothing more from the server. */
export const FINISHED_STATUSES: readonly DocumentStatus[] = [
  "processed",
  "rejected",
  "failed",
];

export const STATUS_LABELS: Readonly<Record<DocumentStatus, string>> = {
  received: "Received",
  scanning: "Scanning",
  extracting: "Extracting",
  classifying: "Classifying",
  parsing: "Parsing",
  needs_review: "Needs review",
  processed: "Imported",
  rejected: "Rejected",
  failed: "Failed",
};

export type DetectedAccount = Schemas["DetectedAccount"];
export type DocumentAccounts = Schemas["DocumentAccounts"];
export type AccountChoice = Schemas["AccountChoice"];

export const documentAccountKeys = {
  one: (id: string) => [...queryRoots.documents, "accounts", id],
} as const;

/** The accounts a document names: matched, or new with a proposal. */
export function getDocumentAccounts(id: string): Promise<DocumentAccounts> {
  return unwrap(
    api.GET("/api/v1/documents/{document_id}/accounts", {
      params: { path: { document_id: id } },
    }),
  );
}

/** Use or create an account for each, then re-import the document (a job). */
export function applyDocumentAccounts(
  id: string,
  items: AccountChoice[],
): Promise<Schemas["DocumentAccountsApplied"]> {
  return unwrap(
    api.POST("/api/v1/documents/{document_id}/accounts/apply", {
      params: { path: { document_id: id } },
      body: { items },
    }),
  );
}

export type LearnedLayout = Schemas["LearnedLayoutOut"];

export const learnedKeys = {
  layouts: [...queryRoots.documents, "layouts"],
} as const;

/** The layouts the household taught PerFin ("Remember this layout", "Teach PerFin"). */
export function listLayouts(): Promise<LearnedLayout[]> {
  return unwrap(api.GET("/api/v1/imports/layouts")).then((list) => list.items);
}

/** Forget a layout, every version of it. */
export async function deleteLayout(id: string): Promise<void> {
  await unwrap(
    api.DELETE("/api/v1/imports/layouts/{layout_id}", {
      params: { path: { layout_id: id } },
    }),
  );
}

export function renameMapping(id: string, name: string): Promise<Mapping> {
  return unwrap(
    api.PATCH("/api/v1/imports/mappings/{mapping_id}", {
      params: { path: { mapping_id: id } },
      body: { name },
    }),
  );
}

export async function deleteMapping(id: string): Promise<void> {
  await unwrap(
    api.DELETE("/api/v1/imports/mappings/{mapping_id}", {
      params: { path: { mapping_id: id } },
    }),
  );
}
