// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { lookupHandlers, NOW } from "../transactions/fixtures";
import { uploadDocument } from "./api";
import { readAsActivity } from "./UploadPage";

class FakeXhr {
  static last: FakeXhr | null = null;
  status = 0;
  statusText = "";
  responseText = "";
  withCredentials = false;
  headers = new Map<string, string>();
  upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
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
    const handle = uploadDocument(new File(["a,b"], "s.csv", { type: "text/csv" }), {
      accountId: "a-1",
      onProgress: (fraction) => progress.push(fraction),
      createRequest: () => new FakeXhr() as unknown as XMLHttpRequest,
    });
    await waitFor(() => {
      expect(FakeXhr.last?.body).toBeInstanceOf(FormData);
    });
    const xhr = present(FakeXhr.last);
    expect(xhr.url).toBe("/api/v1/documents?account_id=a-1");
    expect(xhr.headers.get("X-CSRF-Token")).toBeTruthy();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 4 });
    xhr.status = 202;
    xhr.responseText = JSON.stringify({ id: "d-1", status: "received", duplicate_of: null, job_id: "j-1" });
    xhr.onload?.();
    await expect(handle.promise).resolves.toMatchObject({ id: "d-1" });
    expect(progress).toEqual([0.25]);
  });

  it("rejects with the problem details of a refusal", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    const handle = uploadDocument(new File(["x"], "x.exe"), { createRequest: () => new FakeXhr() as unknown as XMLHttpRequest });
    await waitFor(() => {
      expect(FakeXhr.last?.body).toBeInstanceOf(FormData);
    });
    const failing = present(FakeXhr.last);
    failing.status = 415;
    failing.responseText = JSON.stringify({ type: "urn:perfin:problem:unsupported-file-type", title: "Unsupported", status: 415 });
    failing.onload?.();
    await expect(handle.promise).rejects.toMatchObject({ status: 415, slug: "unsupported-file-type" });
  });
});

describe("upload page", () => {
  const doc = (id: string, status: string, name: string) => ({
    id, status, original_filename: name, kind: "statement", source: "upload", size: 10, mime_detected: "text/csv", sha256: "0".repeat(64),
    uploaded_by: null, account_id: null, institution_id: null, period_start: null, period_end: null, tax_year: null, parser_version: null,
    error: null, created_at: NOW, updated_at: NOW,
  });

  it("lists uploads with their status and links a file that needs mapping", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/documents": () => json({ items: [doc("d-1", "needs_review", "bank.csv"), doc("d-2", "processed", "march.ofx")], next_cursor: null }),
    });
    renderApp("/documents");
    expect(await screen.findByText("bank.csv")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("Imported")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Map columns" })).toHaveAttribute("href", "/documents/d-1/mapping");
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
    expect(screen.getByRole("progressbar", { name: "Uploading new.csv" })).toBeInTheDocument();
    const sent = present(xhrs[0]);
    sent.status = 202;
    sent.responseText = JSON.stringify({ id: "d-3", status: "received", duplicate_of: null, job_id: "j" });
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
    expect(await screen.findByText("You have read-only access.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose files")).not.toBeInTheDocument();
  });
});

describe("readAsActivity", () => {
  it("is true only for documents the brokerage activity reader imported", () => {
    expect(readAsActivity({ parser_version: "brokerage_activity_table/1" })).toBe(true);
    expect(readAsActivity({ parser_version: "delimited/1" })).toBe(false);
    expect(readAsActivity({ parser_version: null })).toBe(false);
  });
});
