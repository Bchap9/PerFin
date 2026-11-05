// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { referenceHandlers } from "../planning/testing";

const saved = vi.hoisted(() => ({ saveBlob: vi.fn() }));
vi.mock("./saveFile", async (importOriginal) => {
  const original = await importOriginal<typeof import("./saveFile")>();
  return { ...original, saveBlob: saved.saveBlob };
});

const STAMP = "2026-11-03T10:00:00Z";

function exportJob(state: string, extra: Record<string, unknown> = {}) {
  return {
    job_id: "x-1",
    state,
    counts: { transactions: 12 },
    created_at: STAMP,
    downloaded_at: null,
    error: null,
    expires_at: "2026-11-03T11:00:00Z",
    finished_at: state === "pending" ? null : STAMP,
    passphrase_protected: false,
    sha256: state === "ready" ? "ab".repeat(32) : null,
    size: state === "ready" ? 2_097_152 : null,
    ...extra,
  };
}

async function stepUp(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), "246810");
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
}

function dataApi(extra: Record<string, Handler> = {}) {
  const state = { stepUps: 0, polls: 0 };
  const api = mockApi({
    "GET /api/v1/auth/session": (call) => authedSession({ role: "owner", csrfToken: `token-${String(state.stepUps)}` })(call),
    ...referenceHandlers(),
    "GET /api/v1/auth/mfa": () => json({ methods: [{ id: "t-1", type: "totp", name: "Phone", created_at: STAMP, last_used_at: null }], recovery_codes_remaining: 10, required: true }),
    "POST /api/v1/auth/step-up": () => {
      state.stepUps += 1;
      return json({ method: "totp", mfa_verified_at: STAMP, csrf_token: `token-${String(state.stepUps)}` });
    },
    ...extra,
  });
  return { api, state };
}

describe("export", () => {
  it("starts an export, follows it, and downloads it with a second step-up", async () => {
    saved.saveBlob.mockClear();
    const { api, state } = dataApi({
      "POST /api/v1/export": () => (state.stepUps < 1 ? problem(403, "step-up-required") : json({ job_id: "x-1", state: "pending", passphrase_protected: true }, 202)),
      "GET /api/v1/export/:id": () => {
        state.polls += 1;
        return json(state.polls < 2 ? exportJob("pending") : exportJob("ready", { passphrase_protected: true }));
      },
      "GET /api/v1/export/:id/download": () =>
        state.stepUps < 2 ? problem(403, "step-up-required") : new Response("ARCHIVE", { status: 200, headers: { "content-type": "application/octet-stream" } }),
    });
    renderApp("/settings/data");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Passphrase/), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Start export" }));
    await stepUp(user);

    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/export").at(-1)?.body).toEqual({ passphrase: "correct horse battery" });
    // The unauthenticated first try and the retry after step-up, with the rotated CSRF token.
    expect(api.callsTo("POST", "/api/v1/export")).toHaveLength(2);
    expect(api.callsTo("POST", "/api/v1/export")[1]?.headers.get("x-csrf-token")).toBe("token-1");

    const download = await screen.findByRole("button", { name: "Download archive" }, { timeout: 4000 });
    expect(screen.getByText(/2\.0 MB, encrypted with your passphrase/)).toBeInTheDocument();
    expect(screen.getByText("ab".repeat(32))).toBeInTheDocument();
    expect(saved.saveBlob).not.toHaveBeenCalled();

    await user.click(download);
    await stepUp(user);
    await waitFor(() => {
      expect(saved.saveBlob).toHaveBeenCalledTimes(1);
    });
    const [blob, filename] = saved.saveBlob.mock.calls[0] as [Blob, string];
    expect(await blob.text()).toBe("ARCHIVE");
    expect(filename).toMatch(/^perfin-export-\d{4}-\d{2}-\d{2}\.pfarch$/);
    expect(await screen.findByText("The archive was downloaded. It cannot be downloaded again.")).toBeInTheDocument();
  });

  it("does nothing when the step-up dialog is cancelled", async () => {
    const { api } = dataApi({ "POST /api/v1/export": () => problem(403, "step-up-required") });
    renderApp("/settings/data");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start export" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.callsTo("POST", "/api/v1/export")).toHaveLength(1);
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.queryByText(/step-up-required/)).not.toBeInTheDocument();
  });

  it("shows why an export failed", async () => {
    dataApi({
      "POST /api/v1/export": () => json({ job_id: "x-1", state: "pending", passphrase_protected: false }, 202),
      "GET /api/v1/export/:id": () => json(exportJob("failed", { error: "disk full" })),
    });
    renderApp("/settings/data");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start export" }));
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
  });
});

describe("import", () => {
  it("uploads the archive with its passphrase header after step-up and shows the summary", async () => {
    const { api, state } = dataApi({
      "POST /api/v1/import": () => (state.stepUps < 1 ? problem(403, "step-up-required") : json({ job_id: "i-1", state: "pending", passphrase_protected: true }, 202)),
      "GET /api/v1/import/:id": () =>
        json({ job_id: "i-1", state: "imported", created_at: STAMP, finished_at: STAMP, error: null, error_detail: null, passphrase_protected: true, summary: { transactions_created: 12, accounts_created: 2 } }),
    });
    renderApp("/settings/data");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Import archive" });
    expect(button).toBeDisabled();
    await user.upload(screen.getByLabelText("Archive file"), new File(["data"], "export.pfarch", { type: "application/octet-stream" }));
    await user.type(screen.getByLabelText("Archive passphrase"), "s3cret");
    await user.click(button);
    await stepUp(user);
    const summary = await screen.findByRole("list", { name: "Import summary" });
    expect(within(summary).getByText("transactions created: 12")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/import");
    expect(calls.at(-1)?.headers.get("x-archive-passphrase")).toBe("s3cret");
    expect(calls.at(-1)?.headers.get("content-type")).toMatch(/^multipart\/form-data/);
  });
});
