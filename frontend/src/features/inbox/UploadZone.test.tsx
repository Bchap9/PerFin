// SPDX-License-Identifier: AGPL-3.0-or-later
// The upload tests moved here from the old Documents page (stage 3, Task 8):
// the uploader is on the Inbox now, so they render `/inbox`.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import { readAsActivity, readAsPositions, uploadDocument } from "../imports/api";
import type { ReviewCard } from "../review/api";
import { account, lookupHandlers, NOW, summary } from "../transactions/fixtures";

// Invented fixtures: no real people, banks or account numbers.

class FakeXhr {
  static last: FakeXhr | null = null;
  status = 0;
  statusText = "";
  responseText = "";
  withCredentials = false;
  headers = new Map<string, string>();
  upload: {
    onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null;
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
    await expect(handle.promise).rejects.toMatchObject({ status: 415, slug: "unsupported-file-type" });
  });
});

function waitingCard(documentId: string, name: string): ReviewCard {
  const doc: ReviewCard["document"] = {
    id: documentId,
    original_filename: name,
    kind: "statement",
    status: "needs_review",
    mime_detected: "text/csv",
    source: "upload",
    created_at: NOW,
    period_start: null,
    period_end: null,
    previewable: false,
  };
  return {
    document: doc,
    items: [
      {
        id: `r-${documentId}`,
        kind: "unparsed",
        state: "open",
        document_id: documentId,
        document: doc,
        payload: { reason: "format_not_supported" },
        resolution: null,
        resolved_at: null,
        resolved_by: null,
        created_at: NOW,
        next_step: null,
        can_resolve: true,
      },
    ],
    action: "map_columns",
    action_item_id: `r-${documentId}`,
    open: 1,
    notices: 0,
  };
}

/** The Inbox with nothing else on it, so the upload zone is what is tested. */
function zoneApi(role: SessionOptions["role"] = "member", extra: Record<string, Handler> = {}) {
  const page = () => json({ items: [], next_cursor: null });
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers([account("a-1", "Everyday")]),
    "GET /api/v1/documents": page,
    "GET /api/v1/review/cards": () => json({ cards: [], loose: [], next_cursor: null }),
    "GET /api/v1/subscriptions/events": () => json({ items: [], next_cursor: null, open: 0 }),
    "GET /api/v1/subscriptions": page,
    "GET /api/v1/inbox/summary": () =>
      json({ documents: 0, loose: 0, subscriptions: 0, suggestions: 0, uncategorised: 0, cards: 0 }),
    "GET /api/v1/categorisation/summary": () => json(summary({ suggestions: 0, uncategorised: 0 })),
    "GET /api/v1/categorisation/learned": page,
    ...extra,
  });
}

function fakeUploads(): FakeXhr[] {
  const xhrs: FakeXhr[] = [];
  vi.stubGlobal("XMLHttpRequest", function Fake() {
    const x = new FakeXhr();
    xhrs.push(x);
    return x;
  });
  return xhrs;
}

function finish(xhr: FakeXhr | undefined, id: string, duplicateOf: string | null = null): void {
  const sent = present(xhr);
  sent.status = 202;
  sent.responseText = JSON.stringify({ id, status: "received", duplicate_of: duplicateOf, job_id: "j" });
  sent.onload?.();
}

describe("upload zone", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uploads a chosen file", async () => {
    zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    const input = await screen.findByLabelText("Choose files");
    await user.upload(input, new File(["a"], "new.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.body).toBeInstanceOf(FormData);
    });
    expect(screen.getByRole("progressbar", { name: "Uploading new.csv" })).toBeInTheDocument();
    finish(xhrs[0], "d-3");
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
  });

  it("a finished file links to its document", async () => {
    zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("Choose files"), new File(["a"], "new.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.body).toBeInstanceOf(FormData);
    });
    finish(xhrs[0], "d-3");
    const uploads = within(await screen.findByRole("list", { name: "Uploads" }));
    expect(await uploads.findByRole("link", { name: "new.csv" })).toHaveAttribute("href", "/inbox/documents/d-3");
  });

  it("a file uploaded before says so", async () => {
    zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("Choose files"), new File(["a"], "again.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.body).toBeInstanceOf(FormData);
    });
    finish(xhrs[0], "d-1", "d-1");
    expect(
      await screen.findByText("This file was uploaded before, so it was not imported again."),
    ).toBeInTheDocument();
  });

  it("a file that needs a person points to its card below", async () => {
    const api = zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("Choose files"), new File(["a"], "odd.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.body).toBeInstanceOf(FormData);
    });
    // The server has read the file and found a question for a person.
    api.on("GET /api/v1/review/cards", () =>
      json({ cards: [waitingCard("d-7", "odd.csv")], loose: [], next_cursor: null }),
    );
    finish(xhrs[0], "d-7");
    const uploads = within(await screen.findByRole("list", { name: "Uploads" }));
    // The cards are above the uploader.
    const above = await uploads.findByRole("link", { name: "Waiting on you above" });
    expect(above).toHaveAttribute("href", "/inbox#inbox-document-d-7");
    // The card it points to carries that anchor.
    const waiting = await screen.findByRole("list", { name: "Waiting on you" });
    expect(waiting.querySelector("#inbox-document-d-7")).not.toBeNull();
  });

  it("a file that was read says whether it was imported", async () => {
    const api = zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText("Choose files"), [
      new File(["a"], "clean.csv", { type: "text/csv" }),
      new File(["b"], "blurry.png", { type: "image/png" }),
    ]);
    await waitFor(() => {
      expect(xhrs[1]?.body).toBeInstanceOf(FormData);
    });
    const row = (id: string, status: string) => ({
      id,
      status,
      original_filename: null,
      kind: "statement",
      source: "upload",
      size: 1,
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
      unchecked: false,
      created_at: NOW,
      updated_at: NOW,
    });
    api.on("GET /api/v1/documents", () =>
      json({ items: [row("d-4", "processed"), row("d-5", "failed")], next_cursor: null }),
    );
    finish(xhrs[0], "d-4");
    finish(xhrs[1], "d-5");
    const uploads = within(await screen.findByRole("list", { name: "Uploads" }));
    const clean = (await uploads.findByRole("link", { name: "clean.csv" })).closest("li");
    const blurry = (await uploads.findByRole("link", { name: "blurry.png" })).closest("li");
    expect(within(present(clean)).getByText("Imported")).toBeInTheDocument();
    expect(within(present(blurry)).getByText("Could not be read")).toBeInTheDocument();
  });

  it("the Upload button opens the file picker", async () => {
    zoneApi();
    renderApp("/inbox");
    const user = userEvent.setup();
    const input = await screen.findByLabelText("Choose files");
    const opened = vi.fn();
    input.addEventListener("click", opened);
    await user.click(screen.getByRole("button", { name: "Upload" }));
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("puts what needs you first, the uploader under it and Upload at the top", async () => {
    zoneApi();
    renderApp("/inbox");
    const clear = await screen.findByRole("region", { name: "All clear" });
    const zone = screen.getByRole("region", { name: "Add documents" });
    const recent = await screen.findByRole("region", { name: "Imported recently" });
    const upload = screen.getByRole("button", { name: "Upload" });
    const before = (a: Element, b: Element) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(before(upload, clear)).toBe(true);
    expect(before(clear, zone)).toBe(true);
    expect(before(zone, recent)).toBe(true);
    // One Upload button: the card keeps its drop zone and "Choose files".
    expect(screen.getAllByRole("button", { name: "Upload" })).toHaveLength(1);
  });

  it("the address #inbox-add-documents brings the uploader into focus", async () => {
    zoneApi();
    renderApp("/inbox#inbox-add-documents");
    const zone = await screen.findByRole("region", { name: "Add documents" });
    await waitFor(() => {
      expect(zone.parentElement).toHaveFocus();
    });
  });

  it("sends the account chosen for the upload", async () => {
    zoneApi();
    const xhrs = fakeUploads();
    renderApp("/inbox");
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "Everyday" });
    await user.selectOptions(screen.getByLabelText("Account"), "a-1");
    await user.upload(screen.getByLabelText("Choose files"), new File(["a"], "new.csv", { type: "text/csv" }));
    await waitFor(() => {
      expect(xhrs[0]?.url).toBe("/api/v1/documents?account_id=a-1");
    });
  });

  it("offers no upload to a viewer", async () => {
    zoneApi("viewer");
    renderApp("/inbox");
    expect(await screen.findByText("You have read-only access.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose files")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });
});

describe("readAsActivity", () => {
  it("is true only for documents the brokerage activity reader imported", () => {
    expect(readAsActivity({ parser_version: "brokerage_activity_table/1" })).toBe(true);
    expect(readAsActivity({ parser_version: "delimited/1" })).toBe(false);
    expect(readAsActivity({ parser_version: null })).toBe(false);
  });
});

describe("readAsPositions", () => {
  it("is true only for documents the brokerage positions reader imported", () => {
    expect(readAsPositions({ parser_version: "brokerage_positions_table/1" })).toBe(true);
    expect(readAsPositions({ parser_version: "brokerage_activity_table/1" })).toBe(false);
    expect(readAsPositions({ parser_version: null })).toBe(false);
  });
});
