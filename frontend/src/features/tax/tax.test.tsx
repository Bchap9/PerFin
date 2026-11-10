// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, currentHref, json, mockApi, noContent, problem, renderApp } from "../auth/testing";
import { CAT_FOOD, money, ownerSession, referenceHandlers } from "../planning/testing";
import type { ChecklistItem, TaxYear } from "./api";
import { openMissing, reminderText } from "./api";

const saved = vi.hoisted(() => ({ saveBlob: vi.fn() }));
vi.mock("../settings/saveFile", async (importOriginal) => {
  const original = await importOriginal<typeof import("../settings/saveFile")>();
  return { ...original, saveBlob: saved.saveBlob };
});

const STAMP = "2026-11-01T10:00:00Z";
const DOC = "d0000000-0000-4000-8000-0000000000aa";

function year(patch: Partial<TaxYear> = {}): TaxYear {
  return {
    id: "ty-1",
    year: 2025,
    jurisdiction: "US",
    filing_status: "single",
    notes: null,
    state: "open",
    checklist: { expected: 1, received: 1, not_applicable: 0 },
    created_at: STAMP,
    updated_at: STAMP,
    ...patch,
  };
}

function item(id: string, form: string, status: ChecklistItem["status"], extra: Partial<ChecklistItem> = {}): ChecklistItem {
  return {
    id,
    expected_form: form,
    form_title: null,
    issuer: null,
    origin: "manual",
    status,
    document_id: null,
    created_at: STAMP,
    updated_at: STAMP,
    ...extra,
  };
}

const ITEMS = [
  item("i-1", "W-2", "received", { issuer: "Acme Corp", document_id: DOC, form_title: "Wage and Tax Statement" }),
  item("i-2", "1099-INT", "expected", { issuer: "First Bank" }),
];

function taxHandlers(extra: Record<string, Parameters<typeof mockApi>[0][string]> = {}) {
  return {
    ...ownerSession(),
    ...referenceHandlers(),
    "GET /api/v1/tax-years": () => json({ items: [year()] }),
    "GET /api/v1/tax-years/:year": () => json(year()),
    "GET /api/v1/tax-years/:year/checklist": () => json({ year: 2025, items: ITEMS, counts: { expected: 1, received: 1, not_applicable: 0 } }),
    "GET /api/v1/tax-years/:year/forms": () =>
      json({
        year: 2025,
        items: [
          {
            id: "f-1",
            document_id: DOC,
            form_type: "W-2",
            form_index: 0,
            tax_year: 2025,
            issuer_name: "Acme Corp",
            source: "parsed",
            confidence: "0.99",
            boxes: [{ box: "1", label: "Wages, tips, other compensation", amount: "52000.00" }],
          },
        ],
      }),
    "GET /api/v1/tax-years/:year/summary": () =>
      json({
        year: 2025,
        jurisdiction: "US",
        currency: "USD",
        partial: false,
        by_treatment: { deductible: money("1200.00") },
        categories: [
          { category_id: CAT_FOOD, category_name: "Charity", tax_treatment: "deductible", schedule: "Schedule A", line: "Line 12", total: money("1200.00"), flagged: money("0.00") },
        ],
        by_hint: [{ schedule: "Schedule A", line: "Line 12", total: money("1200.00") }],
        forms: [],
        checklist: { expected: 1, received: 1, not_applicable: 0 },
        unconverted: [],
      }),
    ...extra,
  };
}

describe("tax reminders", () => {
  it("words the missing count and lists open years that still wait for documents", () => {
    expect(reminderText(0)).toBeNull();
    expect(reminderText(1)).toBe("1 expected document still missing");
    expect(reminderText(3)).toBe("3 expected documents still missing");
    expect(openMissing([year(), year({ year: 2024, state: "filed" }), year({ year: 2023, checklist: { expected: 0, received: 2, not_applicable: 0 } })])).toEqual([
      { year: 2025, missing: 1 },
    ]);
  });
});

describe("tax years page", () => {
  it("lists years with the reminder and opens a new year", async () => {
    const api = mockApi(taxHandlers({ "POST /api/v1/tax-years": () => json(year({ year: 2026 }), 201) }));
    renderApp("/tax");
    const user = userEvent.setup();
    expect(await screen.findByRole("link", { name: "2025" })).toBeInTheDocument();
    expect(screen.getByText(/1 expected document still missing/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open tax year" }));
    const dialog = await screen.findByRole("dialog", { name: "Open a tax year" });
    const field = within(dialog).getByLabelText(/^Year/);
    await user.clear(field);
    await user.type(field, "2026");
    await user.click(within(dialog).getByRole("button", { name: "Open year" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/tax-years")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/tax-years")[0]?.body).toEqual({ year: 2026, jurisdiction: "US" });
  });
});

describe("tax year page checklist", () => {
  it("shows issuer, linked document and amounts by box label", async () => {
    mockApi(taxHandlers());
    renderApp("/tax/2025");
    expect(await screen.findByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open document" })).toHaveAttribute("href", `/documents/${DOC}`);
    expect(await screen.findByText("Wages, tips, other compensation")).toBeInTheDocument();
    expect(screen.getByText("52000.00")).toBeInTheDocument();
    expect(screen.getByText(/1 expected document still missing/)).toBeInTheDocument();
    expect(screen.getAllByText("Schedule A Line 12")).toHaveLength(2);
  });

  it("changes an item's status and sends only the status", async () => {
    const api = mockApi(
      taxHandlers({ "PATCH /api/v1/tax-years/:year/checklist/:id": () => json(item("i-2", "1099-INT", "not_applicable")) }),
    );
    renderApp("/tax/2025");
    const user = userEvent.setup();
    const select = await screen.findByRole("combobox", { name: "Status of 1099-INT from First Bank" });
    await user.selectOptions(select, "not_applicable");
    await vi.waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/tax-years/2025/checklist/i-2")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/tax-years/2025/checklist/i-2")[0]?.body).toEqual({ status: "not_applicable" });
  });

  it("adds an expected document", async () => {
    const api = mockApi(taxHandlers({ "POST /api/v1/tax-years/:year/checklist": () => json(item("i-3", "1098", "expected"), 201) }));
    renderApp("/tax/2025");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add document" }));
    const dialog = await screen.findByRole("dialog", { name: "Add an expected document" });
    await user.click(within(dialog).getByRole("button", { name: "Add to checklist" }));
    expect(await within(dialog).findByText("Choose a form.")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText(/^Form/), "1098");
    await user.type(within(dialog).getByLabelText(/^Issuer/), "Home Lender");
    await user.click(within(dialog).getByRole("button", { name: "Add to checklist" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/tax-years/2025/checklist")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/tax-years/2025/checklist")[0]?.body).toEqual({ expected_form: "1098", issuer: "Home Lender" });
  });

  it("is read only for a viewer and for a closed year", async () => {
    mockApi(taxHandlers({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }) }));
    const { unmount } = renderApp("/tax/2025");
    expect(await screen.findByText("Acme Corp")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Status of/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add document" })).not.toBeInTheDocument();
    expect(screen.getByText("Only an owner or admin can export a tax year.")).toBeInTheDocument();
    unmount();

    mockApi(taxHandlers({ "GET /api/v1/tax-years/:year": () => json(year({ state: "closed" })) }));
    renderApp("/tax/2025");
    expect(await screen.findByText("Acme Corp")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Status of/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add document" })).not.toBeInTheDocument();
  });
});

describe("deleting a tax year", () => {
  it("confirms, deletes the year and returns to the list", async () => {
    const state = { deleted: false };
    const api = mockApi(
      taxHandlers({
        "GET /api/v1/tax-years": () => json({ items: state.deleted ? [] : [year()] }),
        "DELETE /api/v1/tax-years/:year": () => {
          state.deleted = true;
          return noContent();
        },
      }),
    );
    const { router } = renderApp("/tax/2025");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete tax year" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete tax year 2025?" });
    expect(dialog).toHaveTextContent("its checklist of 2 documents");
    expect(dialog).toHaveTextContent("Uploaded documents, the amounts read from them and your transactions are kept.");
    expect(api.callsTo("DELETE", "/api/v1/tax-years/:year")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete tax year" }));
    await vi.waitFor(() => {
      expect(currentHref(router)).toBe("/tax");
    });
    const calls = api.callsTo("DELETE", "/api/v1/tax-years/2025");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Tax year 2025 deleted")).toBeInTheDocument();
    expect(await screen.findByText("No tax years yet")).toBeInTheDocument();
    // The deleted year is not asked for again on the way out.
    expect(api.callsTo("GET", "/api/v1/tax-years/2025")).toHaveLength(1);
  });

  it("shows the server's refusal and stays on the year", async () => {
    mockApi(taxHandlers({ "DELETE /api/v1/tax-years/:year": () => problem(403, "forbidden") }));
    const { router } = renderApp("/tax/2025");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete tax year" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete tax year 2025?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete tax year" }));
    expect(await within(dialog).findByText("detail of forbidden")).toBeInTheDocument();
    expect(currentHref(router)).toBe("/tax/2025");
  });

  it("is offered to owners and admins only", async () => {
    mockApi(taxHandlers({ "GET /api/v1/auth/session": authedSession({ role: "member" }) }));
    renderApp("/tax/2025");
    expect(await screen.findByRole("button", { name: "Mark as filed" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete tax year" })).not.toBeInTheDocument();
  });
});

describe("accountant export", () => {
  it("starts with step-up, follows the job and downloads the zip once with a second step-up", async () => {
    saved.saveBlob.mockClear();
    const state = { stepUps: 0, polls: 0 };
    const api = mockApi(
      taxHandlers({
        "GET /api/v1/auth/session": (call) => authedSession({ role: "owner", csrfToken: `token-${String(state.stepUps)}` })(call),
        "GET /api/v1/auth/mfa": () => json({ methods: [{ id: "t-1", type: "totp", name: "Phone", created_at: STAMP, last_used_at: null }], recovery_codes_remaining: 10, required: true }),
        "POST /api/v1/auth/step-up": () => {
          state.stepUps += 1;
          return json({ method: "totp", mfa_verified_at: STAMP, csrf_token: `token-${String(state.stepUps)}` });
        },
        "POST /api/v1/tax-years/:year/export": () =>
          state.stepUps < 1 ? problem(403, "step-up-required") : json({ job_id: "x-1", year: 2025, state: "pending" }, 202),
        "GET /api/v1/tax-years/:year/export/:job": () => {
          state.polls += 1;
          const ready = state.polls > 1;
          return json({
            job_id: "x-1",
            year: 2025,
            state: ready ? "ready" : "pending",
            size: ready ? 2048 : null,
            sha256: null,
            counts: {},
            error: null,
            created_at: STAMP,
            finished_at: null,
            downloaded_at: null,
            expires_at: null,
          });
        },
        "GET /api/v1/tax-years/:year/export/:job/download": () =>
          state.stepUps < 2 ? problem(403, "step-up-required") : new Response("ZIPDATA", { status: 200, headers: { "content-type": "application/zip" } }),
      }),
    );
    renderApp("/tax/2025");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start export" }));
    await stepUp(user);
    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/tax-years/2025/export")).toHaveLength(2);
    expect(await screen.findByText("Ready to download", undefined, { timeout: 4000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download zip" }));
    await stepUp(user);
    await vi.waitFor(() => {
      expect(saved.saveBlob).toHaveBeenCalledTimes(1);
    });
    const [blob, filename] = saved.saveBlob.mock.calls[0] as [Blob, string];
    expect(filename).toBe("perfin-tax-2025.zip");
    expect(await blob.text()).toBe("ZIPDATA");
  });
});

async function stepUp(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), "246810");
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
}
