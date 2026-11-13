// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { lookupHandlers, NOW } from "../transactions/fixtures";
import { uploadDocument } from "./api";
import {
  mappableDocuments,
  readAsActivity,
  readAsPositions,
} from "./UploadPage";

class FakeXhr {
  static last: FakeXhr | null = null;
  status = 0;
  statusText = "";
  responseText = "";
  withCredentials = false;
  headers = new Map<string, string>();
  upload: {
    onprogress:
      | ((event: {
          lengthComputable: boolean;
          loaded: number;
          total: number;
        }) => void)
      | null;
  } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  url = "";
  body: unknown = null;
  constructor() {
    FakeXhr.last = this;
  }
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers.set(name, value);
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.onabort?.();
  }
}

function present<T>(value: T | null | undefined): T {
  if (value === null || value === undefined) {
    throw new Error("expected a value");
  }
  return value;
}

describe("uploadDocument", () => {
  it("reports progress and resolves with the received document", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    const progress: number[] = [];
    const handle = uploadDocument(
      new File(["a,b"], "s.csv", { type: "text/csv" }),
      {
        accountId: "a-1",
        onProgress: (fraction) => progress.push(fraction),
        createRequest: () => new FakeXhr() as unknown as XMLHttpRequest,
      },
    );
    await waitFor(() => {
      expect(FakeXhr.last?.body).toBeInstanceOf(FormData);
    });
    const xhr = present(FakeXhr.last);
    expect(xhr.url).toBe("/api/v1/documents?account_id=a-1");
    expect(xhr.headers.get("X-CSRF-Token")).toBeTruthy();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 4 });
    xhr.status = 202;
    xhr.responseText = JSON.stringify({
      id: "d-1",
      status: "received",
      duplicate_of: null,
      job_id: "j-1",
    });
    xhr.onload?.();
    await expect(handle.promise).resolves.toMatchObject({ id: "d-1" });
    expect(progress).toEqual([0.25]);
  });

  it("rejects with the problem details of a refusal", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    const handle = uploadDocument(new File(["x"], "x.exe"), {
      createRequest: () => new FakeXhr() as unknown as XMLHttpRequest,
    });
    await waitFor(() => {
      expect(FakeXhr.last?.body).toBeInstanceOf(FormData);
    });
    const failing = present(FakeXhr.last);
    failing.status = 415;
    failing.responseText = JSON.stringify({
      type: "urn:perfin:problem:unsupported-file-type",
      title: "Unsupported",
      status: 415,
    });
    failing.onload?.();
    await expect(handle.promise).rejects.toMatchObject({
      status: 415,
      slug: "unsupported-file-type",
    });
  });
});

describe("upload page", () => {
  const doc = (id: string, status: string, name: string) => ({
    id,
    status,
    original_filename: name,
    kind: "statement",
    source: "upload",
    size: 10,
    mime_detected: "text/csv",
    sha256: "0".repeat(64),
    uploaded_by: null,
    account_id: null,
    institution_id: null,
    period_start: null,
    period_end: null,
    tax_year: null,
    parser_version: null,
    error: null,
    created_at: NOW,
    updated_at: NOW,
  });

  const waiting = (
    id: string,
    documentId: string,
    reason: string,
    nextStep: string,
    kind = "unparsed",
  ) => ({
    id,
    kind,
    state: "open",
    document_id: documentId,
    payload: { reason },
    reason,
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: nextStep,
    can_resolve: true,
  });

  it("lists uploads with their status and links a file that needs mapping", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () =>
        json({
          items: [
            doc("d-1", "needs_review", "bank.csv"),
            doc("d-2", "processed", "march.ofx"),
          ],
          next_cursor: null,
        }),
      "GET /api/v1/review": () =>
        json({
          items: [waiting("r-1", "d-1", "mapping_required", "column_mapping")],
          next_cursor: null,
        }),
    });
    renderApp("/documents");
    expect(await screen.findByText("bank.csv")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("Imported")).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: "Map columns" }),
    ).toHaveAttribute("href", "/documents/d-1/mapping");
  });

  it("offers Map columns only for files the mapping editor answers", async () => {
    const activity = (id: string, name: string) => ({
      ...doc(id, "needs_review", name),
      parser_version: "brokerage_activity_table/1",
    });
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () =>
        json({
          items: [
            doc("d-1", "needs_review", "bank.csv"),
            doc("d-2", "needs_review", "empty.csv"),
            activity("d-3", "unread-activity.csv"),
            activity("d-4", "activity.csv"),
            doc("d-5", "needs_review", "statement.ofx"),
          ],
          next_cursor: null,
        }),
      "GET /api/v1/review": () =>
        json({
          items: [
            waiting("r-1", "d-1", "mapping_required", "column_mapping"),
            waiting("r-2", "d-2", "no_rows", "reprocess"),
            waiting("r-3", "d-3", "activity_unreadable", "column_mapping"),
            waiting(
              "r-4",
              "d-4",
              "account_not_found",
              "choose_account",
              "unknown_account",
            ),
            waiting(
              "r-5",
              "d-5",
              "lines_skipped",
              "correct_fields",
              "low_confidence_field",
            ),
          ],
          next_cursor: null,
        }),
    });
    renderApp("/documents");
    expect(await screen.findByText("empty.csv")).toBeInTheDocument();
    const uploads = within(screen.getByRole("table"));
    await waitFor(() => {
      expect(
        uploads
          .getAllByRole("link", { name: "Map columns" })
          .map((link) => link.getAttribute("href")),
      ).toEqual(["/documents/d-1/mapping", "/documents/d-3/mapping"]);
    });
    // Every file that waits still leads to the review queue.
    expect(uploads.getAllByRole("link", { name: "Review" })).toHaveLength(5);
    // Activity that was read keeps its note; the unread one is mapped instead.
    expect(uploads.getAllByText("Read as investment activity")).toHaveLength(1);
  });

  it("offers no Map columns while the review queue cannot be read", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () =>
        json({
          items: [doc("d-1", "needs_review", "bank.csv")],
          next_cursor: null,
        }),
      "GET /api/v1/review": () => json({ title: "Unavailable" }, 503),
    });
    renderApp("/documents");
    expect(await screen.findByText("bank.csv")).toBeInTheDocument();
    const uploads = within(screen.getByRole("table"));
    expect(uploads.getByRole("link", { name: "Review" })).toBeInTheDocument();
    expect(uploads.queryByRole("link", { name: "Map columns" })).toBeNull();
  });

  it("uploads a chosen file", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () => json({ items: [], next_cursor: null }),
    });
    const xhrs: FakeXhr[] = [];
    vi.stubGlobal("XMLHttpRequest", function Fake() {
      const x = new FakeXhr();
      xhrs.push(x);
      return x;
    });
    renderApp("/documents");
    const user = userEvent.setup();
    const input = await screen.findByLabelText("Choose files");
    await user.upload(input, new File(["a"], "new.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.body).toBeInstanceOf(FormData);
    });
    expect(
      screen.getByRole("progressbar", { name: "Uploading new.csv" }),
    ).toBeInTheDocument();
    const sent = present(xhrs[0]);
    sent.status = 202;
    sent.responseText = JSON.stringify({
      id: "d-3",
      status: "received",
      duplicate_of: null,
      job_id: "j",
    });
    sent.onload?.();
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
  });

  it("offers no upload to a viewer", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () => json({ items: [], next_cursor: null }),
    });
    renderApp("/documents");
    expect(
      await screen.findByText("You have read-only access."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose files")).not.toBeInTheDocument();
  });
});

describe("mappableDocuments", () => {
  it("names the documents with an item the mapping editor answers", () => {
    expect([
      ...mappableDocuments([
        { document_id: "d-1", next_step: "column_mapping" },
        { document_id: "d-2", next_step: "reprocess" },
        { document_id: null, next_step: "column_mapping" },
        { document_id: "d-3", next_step: null },
      ]),
    ]).toEqual(["d-1"]);
  });
});

describe("readAsActivity", () => {
  it("is true only for documents the brokerage activity reader imported", () => {
    expect(
      readAsActivity({ parser_version: "brokerage_activity_table/1" }),
    ).toBe(true);
    expect(readAsActivity({ parser_version: "delimited/1" })).toBe(false);
    expect(readAsActivity({ parser_version: null })).toBe(false);
  });
});

describe("readAsPositions", () => {
  it("is true only for documents the brokerage positions reader imported", () => {
    expect(
      readAsPositions({ parser_version: "brokerage_positions_table/1" }),
    ).toBe(true);
    expect(
      readAsPositions({ parser_version: "brokerage_activity_table/1" }),
    ).toBe(false);
    expect(readAsPositions({ parser_version: null })).toBe(false);
  });
});
