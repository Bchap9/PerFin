// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../../auth/testing";

const TOKEN = "pfn_airead_SECRETsecretSECRETsecretSECRETsecretSECRETsecret";

const CLIENT = {
  id: "c-1",
  name: "Claude Desktop",
  mode: "detail",
  transport: "stdio",
  rounding: 10,
  hide_nicknames: true,
  hide_merchant_location: false,
  date_range_cap: 90,
  custom_denylist: ["Acme Corp"],
  member_aliases: ["Bobby"],
  token_prefix: "airead",
  status: "active",
  created_at: "2026-11-01T10:00:00Z",
  expires_at: "2026-12-01T10:00:00Z",
  revoked_at: null,
  last_used_at: null,
};

const PREVIEW = {
  mode: "detail",
  placeholder_counts: { "[MEMBER]": 1, "[ADDRESS]": 1 },
  tools: [
    {
      tool: "list_transactions",
      params: { limit: 20 },
      status: 200,
      bytes: 120,
      body: { items: [{ payee: "<img src=x onerror=alert(1)> [MEMBER]" }, { payee: "Cafe [ADDRESS]" }] },
      placeholders: [
        { path: "items[0].payee", placeholders: ["[MEMBER]"] },
        { path: "items[1].payee", placeholders: ["[ADDRESS]"] },
      ],
    },
    { tool: "get_overview", params: {}, status: 200, bytes: 40, body: { net_worth: "1" }, placeholders: [] },
  ],
};

const CALL = {
  id: "call-1",
  client_id: "c-1",
  created_at: "2026-11-03T10:00:00Z",
  tool: "list_transactions",
  params: { limit: 20 },
  status: 200,
  bytes: 321,
  response: { items: [{ payee: "Cafe [ADDRESS]" }] },
};

function aiApi(extra: Record<string, Handler> = {}, session = authedSession({ aiClientsActive: true })) {
  const state = { steppedUp: false, clients: [CLIENT] as unknown[], household: true, system: true };
  const access = () =>
    json({
      enabled: state.household && state.system,
      household_enabled: state.household,
      system_enabled: state.system,
      active_clients: state.clients.length,
    });
  const api = mockApi({
    "GET /api/v1/auth/session": (call) => {
      if (state.steppedUp) {
        return authedSession({ csrfToken: "stepped-token", aiClientsActive: true })(call);
      }
      return session(call);
    },
    "GET /api/v1/households": () => json([]),
    "GET /api/v1/auth/mfa": () =>
      json({ methods: [{ id: "k", type: "totp", name: "Phone", created_at: "2026-10-01T10:00:00Z", last_used_at: null }], recovery_codes_remaining: 7, required: true }),
    "POST /api/v1/auth/step-up": (call) => {
      if ((call.body as { code?: string }).code !== "246810") {
        return problem(400, "mfa-invalid");
      }
      state.steppedUp = true;
      return json({ method: "totp", mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "stepped-token" });
    },
    "GET /api/v1/settings/ai": access,
    "PATCH /api/v1/settings/ai": (call) => {
      state.household = (call.body as { enabled: boolean }).enabled;
      return access();
    },
    "PATCH /api/v1/settings/system/ai": (call) => {
      state.system = (call.body as { enabled: boolean }).enabled;
      return json({ enabled: state.system });
    },
    "GET /api/v1/settings/ai/clients": () => json(state.clients),
    "POST /api/v1/settings/ai/preview": () => json(PREVIEW),
    "GET /api/v1/settings/ai/clients/:id/preview": () => json(PREVIEW),
    ...extra,
  });
  return { api, state };
}

async function stepUpWithTotp(user: ReturnType<typeof userEvent.setup>, code = "246810") {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), code);
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
}

describe("AI access: wizard", () => {
  it("previews with highlighted placeholders, asks for step-up, then shows the token once", async () => {
    const { api, state } = aiApi({
      "POST /api/v1/settings/ai/clients": () => {
        if (!state.steppedUp) {
          return problem(403, "step-up-required");
        }
        return json({ ...CLIENT, id: "c-2", name: "Laptop", token: TOKEN }, 201);
      },
    });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const form = await screen.findByRole("form", { name: "New AI client" });

    // Step 1 validates before moving on.
    await user.click(within(form).getByRole("button", { name: "Next" }));
    expect(await within(form).findByText(/Give the client a name/)).toBeInTheDocument();
    await user.type(within(form).getByLabelText(/^Client name/), "Laptop");
    await user.selectOptions(within(form).getByLabelText("Mode"), "aggregates");
    await user.click(within(form).getByRole("button", { name: "Next" }));

    // Step 2: options and the live preview.
    await user.selectOptions(await within(form).findByLabelText("Round amounts"), "100");
    await user.click(within(form).getByLabelText("Hide account nicknames"));
    await user.type(within(form).getByLabelText("Custom denylist"), "Acme Corp{Enter}Old School");
    await user.type(within(form).getByLabelText(/^Date range limit/), "30");
    const tool = await within(form).findByText("list_transactions", {}, { timeout: 3000 });
    await user.click(tool);
    const sample = await within(form).findByLabelText("Sample answer of list_transactions");
    expect([...sample.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual(["[MEMBER]", "[ADDRESS]"]);
    expect(sample.querySelector("img")).toBeNull();
    await waitFor(() => {
      const last = api.callsTo("POST", "/api/v1/settings/ai/preview").at(-1)?.body;
      expect(last).toEqual({
        name: "Preview",
        mode: "aggregates",
        transport: "stdio",
        rounding: 100,
        hide_nicknames: true,
        hide_merchant_location: false,
        date_range_cap: 30,
        custom_denylist: ["Acme Corp", "Old School"],
        member_aliases: [],
      });
    });
    await user.click(within(form).getByRole("button", { name: "Next" }));

    // Step 3: nothing is saved until step-up succeeds.
    expect(within(form).getByText("Laptop")).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Create client" }));
    await stepUpWithTotp(user, "111111");
    expect(await screen.findByText("That code or key was not accepted. Try again.")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog", { name: "Confirm it is you" });
    await user.clear(within(dialog).getByLabelText(/^Authentication code/));
    await user.type(within(dialog).getByLabelText(/^Authentication code/), "246810");
    await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));

    const shown = await screen.findByRole("dialog", { name: "Your new AI client" });
    const creates = api.callsTo("POST", "/api/v1/settings/ai/clients");
    expect(creates.map((call) => call.headers.get("X-CSRF-Token"))).toEqual(["session-token", "stepped-token"]);
    expect(creates[1]?.body).toMatchObject({
      name: "Laptop",
      mode: "aggregates",
      rounding: 100,
      hide_nicknames: true,
      date_range_cap: 30,
      custom_denylist: ["Acme Corp", "Old School"],
      expires_in_days: 30,
      transport: "stdio",
    });

    // Token shown once, with a copy button and the setup snippet (which never holds the token).
    expect(within(shown).getByText(TOKEN)).toBeInTheDocument();
    const snippet = within(shown).getByLabelText("MCP setup snippet");
    expect(snippet.textContent).toContain("PERFIN_MCP_TOKEN_FILE");
    expect(snippet.textContent).toContain(window.location.origin);
    expect(snippet.textContent).not.toContain(TOKEN);
    await user.click(within(shown).getByRole("button", { name: "Copy token" }));
    expect(await navigator.clipboard.readText()).toBe(TOKEN);

    // It cannot be closed before confirming, and it is gone afterwards.
    expect(within(shown).getByRole("button", { name: "Done" })).toBeDisabled();
    await user.click(within(shown).getByLabelText(/I have stored this somewhere safe/));
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(screen.queryByText(TOKEN)).not.toBeInTheDocument();
    });
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("does not create the client when the step-up dialog is closed", async () => {
    const { api } = aiApi({ "POST /api/v1/settings/ai/clients": () => problem(403, "step-up-required") });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const form = await screen.findByRole("form", { name: "New AI client" });
    await user.type(within(form).getByLabelText(/^Client name/), "Laptop");
    await user.click(within(form).getByRole("button", { name: "Next" }));
    await user.click(await within(form).findByRole("button", { name: "Next" }));
    await user.click(within(form).getByRole("button", { name: "Create client" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Confirm it is you" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("POST", "/api/v1/settings/ai/clients")).toHaveLength(1);
    expect(screen.queryByRole("dialog", { name: "Your new AI client" })).not.toBeInTheDocument();
  });

  it("rejects an expiry beyond the maximum and a bad date range before saving", async () => {
    const { api } = aiApi();
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New client" }));
    const form = await screen.findByRole("form", { name: "New AI client" });
    await user.type(within(form).getByLabelText(/^Client name/), "Laptop");
    await user.clear(within(form).getByLabelText(/^Expires after/));
    await user.type(within(form).getByLabelText(/^Expires after/), "999");
    await user.click(within(form).getByRole("button", { name: "Next" }));
    expect(await within(form).findByText("Choose 1 to 365 days.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/settings/ai/preview")).toHaveLength(0);
  });
});

describe("AI access: clients, switches, calls, scrubber", () => {
  it("lists clients, previews a saved one and revokes after step-up", async () => {
    const { api, state } = aiApi({
      "DELETE /api/v1/settings/ai/clients/:id": () => {
        if (!state.steppedUp) {
          return problem(403, "step-up-required");
        }
        state.clients = [{ ...CLIENT, status: "revoked", revoked_at: "2026-11-04T10:00:00Z" }];
        return new Response(null, { status: 204 });
      },
    });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    const row = (await screen.findByText("Claude Desktop")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Active")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText(/rounded to 10, nicknames hidden, 90 day limit, 1 denylist entries/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Preview Claude Desktop" }));
    const preview = await screen.findByRole("dialog", { name: "What Claude Desktop sees" });
    expect(await within(preview).findByText("list_transactions")).toBeInTheDocument();
    await user.click(within(preview).getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: "Revoke Claude Desktop" }));
    const confirm = await screen.findByRole("dialog", { name: "Revoke Claude Desktop?" });
    await user.click(within(confirm).getByRole("button", { name: "Revoke" }));
    await stepUpWithTotp(user);
    expect(await screen.findByText("Revoked")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/settings/ai/clients/c-1")).toHaveLength(2);
  });

  it("shows the kill switch status and turns the household switch off after step-up", async () => {
    const { state } = aiApi({
      "PATCH /api/v1/settings/ai": (call) => {
        if (!state.steppedUp) {
          return problem(403, "step-up-required");
        }
        state.household = (call.body as { enabled: boolean }).enabled;
        return json({ enabled: false, household_enabled: false, system_enabled: true, active_clients: 1 });
      },
    });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    expect(await screen.findByText("AI access is on")).toBeInTheDocument();
    expect(screen.getByText("Only an instance administrator can change this.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Switch off for this household" }));
    await stepUpWithTotp(user);
    expect(await screen.findByText("AI access is off")).toBeInTheDocument();
  });

  it("lets an instance admin engage the instance kill switch", async () => {
    const { api } = aiApi({}, authedSession({ instanceAdmin: true, aiClientsActive: true }));
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Engage the instance kill switch" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/settings/system/ai")[0]?.body).toEqual({ enabled: false });
    });
    expect(await screen.findByText("AI access is off")).toBeInTheDocument();
  });

  it("searches the call audit to confirm something never left", async () => {
    const { api } = aiApi({
      "GET /api/v1/settings/ai/calls": (call) => {
        const query = new URLSearchParams(call.search);
        const needle = query.get("contains");
        if (needle === "SSN-123") {
          return json({ items: [] });
        }
        return json({ items: [CALL] });
      },
    });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Calls" }));
    expect(await screen.findByText("1 most recent calls.")).toBeInTheDocument();
    await user.click(screen.getByText("list_transactions"));
    const response = await screen.findByLabelText("Response of the list_transactions call");
    expect(response.querySelector("mark")?.textContent).toBe("[ADDRESS]");

    await user.type(screen.getByLabelText("Search the responses"), "SSN-123");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText('No recorded response contains "SSN-123". It was never sent.')).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/settings/ai/calls").at(-1)?.search).toContain("contains=SSN-123");
  });

  it("shows scrubbed text next to the original", async () => {
    const { api } = aiApi({
      "POST /api/v1/settings/ai/scrubber-preview": () =>
        json({
          changed: 1,
          samples: [
            { source: "transaction.memo", original: "Lunch with Bobby <b>x</b>", scrubbed: "Lunch with [MEMBER] <b>x</b>", changed: true, placeholders: ["[MEMBER]"] },
            { source: "payee.name", original: "Cafe", scrubbed: "Cafe", changed: false, placeholders: [] },
          ],
        }),
    });
    renderApp("/settings/ai");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Scrubbed vs original" }));
    await user.selectOptions(await screen.findByLabelText("Start from a client's terms"), "c-1");
    expect(screen.getByLabelText("Denylist terms to try")).toHaveValue("Acme Corp");
    await user.click(screen.getByRole("button", { name: "Show scrubbed text" }));
    expect(await screen.findByText("1 of 2 samples changed.")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("Lunch with Bobby <b>x</b>")).toBeInTheDocument();
    expect(within(table).getByText("[MEMBER]").tagName).toBe("MARK");
    expect(table.querySelector("b")).toBeNull();
    expect(within(table).queryByText("Cafe")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/settings/ai/scrubber-preview")[0]?.body).toMatchObject({
      client_id: "c-1",
      custom_denylist: ["Acme Corp"],
      member_aliases: ["Bobby"],
    });
  });

  it("tells a non-owner why they cannot manage AI access", async () => {
    aiApi({}, authedSession({ role: "member" }));
    renderApp("/settings/ai");
    expect(await screen.findByText(/Only the household owner manages AI access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New client" })).not.toBeInTheDocument();
  });
});

describe("AI banner", () => {
  it("is shown on every page while an AI client is active", async () => {
    aiApi({ "GET /api/v1/settings/user": () => json({}) });
    renderApp("/settings");
    const banner = await screen.findByTestId("ai-banner");
    expect(banner).toHaveTextContent("AI access is on");
    expect(within(banner).getByRole("link", { name: "Review or revoke" })).toHaveAttribute("href", "/settings/ai");
  });

  it("is absent when no client is active", async () => {
    aiApi({}, authedSession({ aiClientsActive: false }));
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByTestId("ai-banner")).not.toBeInTheDocument();
  });
});
