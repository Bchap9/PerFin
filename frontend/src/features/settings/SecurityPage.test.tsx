// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, noContent, problem, renderApp, type Handler } from "../auth/testing";

const webauthn = vi.hoisted(() => ({ startRegistration: vi.fn(), startAuthentication: vi.fn() }));

vi.mock("@simplewebauthn/browser", () => ({
  browserSupportsWebAuthn: () => true,
  startAuthentication: webauthn.startAuthentication,
  startRegistration: webauthn.startRegistration,
}));

const KEY = { id: "k-1", type: "webauthn", name: "Blue key", created_at: "2026-10-01T10:00:00Z", last_used_at: null };
const APP = { id: "t-1", type: "totp", name: "Phone", created_at: "2026-10-01T10:00:00Z", last_used_at: "2026-11-01T10:00:00Z" };

const TOKEN = {
  id: "tok-1",
  name: "Home Assistant",
  prefix: "0123456789ab",
  scopes: ["accounts:read", "budgets:read"],
  active_scopes: ["accounts:read"],
  status: "active",
  created_at: "2026-10-01T10:00:00Z",
  expires_at: "2026-12-01T10:00:00Z",
  last_used_at: null,
  revoked_at: null,
};

const CATALOG = {
  max_expires_in_days: 90,
  scopes: [
    { scope: "accounts:read", description: "Read accounts", grantable: true, reserved: false },
    { scope: "accounts:write", description: "Change accounts", grantable: true, reserved: false },
    { scope: "export", description: "Export everything", grantable: false, reserved: false },
    { scope: "ai:read", description: "AI access", grantable: true, reserved: true },
  ],
};

const SESSIONS = [
  {
    id: "s-1",
    current: true,
    login_method: "local",
    created_at: "2026-11-03T09:00:00Z",
    last_seen_at: "2026-11-03T10:00:00Z",
    expires_at: "2026-11-03T21:00:00Z",
    remember_device: false,
    user_agent: "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/140.0",
    ip: "192.0.2.10",
  },
  {
    id: "s-2",
    current: false,
    login_method: "oidc",
    created_at: "2026-11-02T09:00:00Z",
    last_seen_at: "2026-11-02T10:00:00Z",
    expires_at: "2026-11-02T21:00:00Z",
    remember_device: true,
    user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    ip: null,
  },
];

/**
 * The security page's API. `steppedUp` flips when `POST /auth/step-up`
 * succeeds; the session's CSRF token rotates with it.
 */
function securityApi(extra: Record<string, Handler> = {}) {
  const state = { steppedUp: false, methods: [APP, KEY] as unknown[], recovery: 7 };
  const api = mockApi({
    "GET /api/v1/auth/session": (call) => authedSession({ csrfToken: state.steppedUp ? "stepped-token" : "session-token" })(call),
    "GET /api/v1/households": () => json([]),
    "GET /api/v1/auth/mfa": () => json({ methods: state.methods, recovery_codes_remaining: state.recovery, required: true }),
    "GET /api/v1/auth/sessions": () => json(SESSIONS),
    "GET /api/v1/auth/tokens": () => json([TOKEN]),
    "GET /api/v1/auth/tokens/scopes": () => json(CATALOG),
    "POST /api/v1/auth/step-up": (call) => {
      const body = call.body as { method?: string; code?: string };
      if (body.code !== "246810") {
        return problem(400, "mfa-invalid");
      }
      state.steppedUp = true;
      return json({ method: "totp", mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "stepped-token" });
    },
    ...extra,
  });
  return { api, state };
}

async function stepUpWithTotp(user: ReturnType<typeof userEvent.setup>, code = "246810") {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), code);
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
  return dialog;
}

describe("security settings: step-up", () => {
  it("offers an OIDC session without a PerFin factor a forced sign-in at the identity provider", async () => {
    const { api, state } = securityApi({
      "GET /api/v1/auth/session": authedSession({ authMethod: "oidc", enrolled: false }),
      "POST /api/v1/auth/tokens": () => problem(403, "step-up-required"),
    });
    state.methods = [];
    renderApp("/settings/security");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a personal access token" });
    await user.type(within(form).getByLabelText(/^Token name/), "Script");
    await user.click(within(form).getByLabelText("accounts:read"));
    await user.click(within(form).getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    const link = await within(dialog).findByRole("link", { name: "sign in again with your identity provider" });
    expect(link).toHaveAttribute("href", "/api/v1/auth/oidc/start?return_to=%2Fsettings%2Fsecurity&reauth=true");
    expect(api.callsTo("POST", "/api/v1/auth/step-up")).toHaveLength(0);
  });

  it("does not offer the identity-provider sign-in to a local session", async () => {
    const { state } = securityApi({
      "GET /api/v1/auth/session": authedSession({ authMethod: "local", enrolled: false }),
      "POST /api/v1/auth/tokens": () => problem(403, "step-up-required"),
    });
    state.methods = [];
    renderApp("/settings/security");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a personal access token" });
    await user.type(within(form).getByLabelText(/^Token name/), "Script");
    await user.click(within(form).getByLabelText("accounts:read"));
    await user.click(within(form).getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    expect(await within(dialog).findByText(/your account has none yet/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("link", { name: /identity provider/ })).not.toBeInTheDocument();
  });

  it("asks for step-up, then retries the original request with the rotated CSRF token", async () => {
    let attempts = 0;
    const { api, state } = securityApi({
      "POST /api/v1/auth/tokens": (call) => {
        attempts += 1;
        if (!state.steppedUp) {
          return problem(403, "step-up-required");
        }
        expect(call.body).toEqual({ name: "Script", scopes: ["accounts:read", "accounts:write"], expires_in_days: 30 });
        return json({ ...TOKEN, id: "tok-2", name: "Script", token: "pfn_0123456789ab_SECRETsecretSECRETsecretSECRETsecretSECRE" }, 201);
      },
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a personal access token" });
    // Only grantable, non-reserved scopes are offered.
    expect(within(form).queryByLabelText("export")).not.toBeInTheDocument();
    expect(within(form).queryByLabelText("ai:read")).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText(/^Token name/), "Script");
    await user.click(within(form).getByLabelText("accounts:write"));
    await user.click(within(form).getByLabelText("accounts:read"));
    await user.click(within(form).getByRole("button", { name: "Create token" }));

    // A wrong code keeps the dialog open; the right one continues.
    await stepUpWithTotp(user, "111111");
    expect(await screen.findByText("That code or key was not accepted. Try again.")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog", { name: "Confirm it is you" });
    await user.clear(within(dialog).getByLabelText(/^Authentication code/));
    await user.type(within(dialog).getByLabelText(/^Authentication code/), "246810");
    await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));

    const shown = await screen.findByRole("dialog", { name: "Your new token" });
    expect(within(shown).getByText(/^pfn_0123456789ab_/)).toBeInTheDocument();
    expect(attempts).toBe(2);
    const creates = api.callsTo("POST", "/api/v1/auth/tokens");
    expect(creates.map((call) => call.headers.get("X-CSRF-Token"))).toEqual(["session-token", "stepped-token"]);
    expect(api.callsTo("POST", "/api/v1/auth/step-up").every((call) => call.headers.get("X-CSRF-Token") === "session-token")).toBe(true);

    // Show once: the token goes away after acknowledging.
    await user.click(within(shown).getByLabelText(/I have stored this somewhere safe/));
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(screen.queryByText(/^pfn_0123456789ab_SECRET/)).not.toBeInTheDocument();
    });
  });

  it("does not retry when the step-up dialog is closed", async () => {
    const { api } = securityApi({ "POST /api/v1/auth/mfa/recovery-codes": () => problem(403, "step-up-required") });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Generate new recovery codes" }));
    const confirm = await screen.findByRole("dialog", { name: "Generate new recovery codes?" });
    await user.click(within(confirm).getByRole("button", { name: "Generate" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Confirm it is you" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("POST", "/api/v1/auth/mfa/recovery-codes")).toHaveLength(1);
  });
});

describe("security settings", () => {
  it("changes the password after step-up and keeps the new CSRF token", async () => {
    const { api, state } = securityApi({
      "POST /api/v1/auth/password": () =>
        state.steppedUp ? json({ csrf_token: "after-password", sessions_revoked: 2 }) : problem(403, "step-up-required"),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Current password/), "old password value");
    await user.type(screen.getByLabelText(/^New password/), "a new long passphrase");
    await user.type(screen.getByLabelText(/^Repeat the new password/), "a new long passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await stepUpWithTotp(user);
    expect(await screen.findByText("2 other sessions were signed out.")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/auth/password");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body).toEqual({ current_password: "old password value", new_password: "a new long passphrase" });
    expect(screen.getByLabelText(/^Current password/)).toHaveValue("");
  });

  it("flags a wrong current password on its field", async () => {
    securityApi({ "POST /api/v1/auth/password": () => problem(403, "invalid-credentials") });
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Current password/), "nope nope nope");
    await user.type(screen.getByLabelText(/^New password/), "a new long passphrase");
    await user.type(screen.getByLabelText(/^Repeat the new password/), "a new long passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("The current password is wrong.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Current password/)).toHaveAttribute("aria-invalid", "true");
  });

  it("lists factors, registers a security key and removes one with step-up", async () => {
    webauthn.startRegistration.mockResolvedValue({ id: "new", rawId: "new", type: "public-key", response: {} });
    const { api, state } = securityApi({
      "POST /api/v1/auth/mfa/webauthn/registration-options": () =>
        state.steppedUp ? json({ challenge: "cmVn", rp: { id: "localhost" }, user: { id: "dQ" } }) : problem(403, "step-up-required"),
      "POST /api/v1/auth/mfa/webauthn": () => {
        state.methods = [APP, KEY, { ...KEY, id: "k-2", name: "Laptop" }];
        return json(
          { method: { ...KEY, id: "k-2", name: "Laptop" }, mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "added-token", recovery_codes: null },
          201,
        );
      },
      "DELETE /api/v1/auth/mfa/webauthn/:id": () => {
        state.methods = [APP];
        return noContent();
      },
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    expect(await screen.findByRole("rowheader", { name: "Blue key" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Phone" })).toBeInTheDocument();
    // An authenticator app exists, so only security keys can be added.
    expect(screen.queryByRole("button", { name: "Set up authenticator app" })).not.toBeInTheDocument();

    await user.clear(screen.getByLabelText(/^Key name/));
    await user.type(screen.getByLabelText(/^Key name/), "Laptop");
    await user.click(screen.getByRole("button", { name: "Add security key or passkey" }));
    await stepUpWithTotp(user);
    expect(await screen.findByText("Laptop added")).toBeInTheDocument();
    expect(webauthn.startRegistration).toHaveBeenCalledTimes(1);
    expect(api.callsTo("POST", "/api/v1/auth/mfa/webauthn")[0]?.body).toEqual({
      name: "Laptop",
      credential: { id: "new", rawId: "new", type: "public-key", response: {} },
    });

    await user.click(await screen.findByRole("button", { name: "Remove Blue key" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove Blue key?" });
    await user.click(within(confirm).getByRole("button", { name: "Remove" }));
    await waitFor(() => {
      expect(screen.queryByRole("rowheader", { name: "Blue key" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("DELETE", "/api/v1/auth/mfa/webauthn/:id")[0]?.path).toBe("/api/v1/auth/mfa/webauthn/k-1");
  });

  it("shows why the last factor cannot be removed", async () => {
    const { state } = securityApi({
      "DELETE /api/v1/auth/mfa/totp": () =>
        problem(409, "last-mfa-factor", { detail: "This instance requires a second factor. Add another one first." }),
    });
    state.methods = [APP];
    state.steppedUp = true;
    renderApp("/settings/security");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove Phone" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove Phone?" });
    await user.click(within(confirm).getByRole("button", { name: "Remove" }));
    expect(await within(confirm).findByText(/Add another one first/)).toHaveAttribute("role", "alert");
  });

  it("regenerates recovery codes and shows them once", async () => {
    const { state } = securityApi({
      "POST /api/v1/auth/mfa/recovery-codes": () => {
        state.recovery = 10;
        return json({ codes: ["1111-2222-3333-4444", "5555-6666-7777-8888"] });
      },
    });
    state.steppedUp = true;
    renderApp("/settings/security");
    const user = userEvent.setup();
    expect(await screen.findByText("7 unused")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Generate new recovery codes" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Generate new recovery codes?" })).getByRole("button", { name: "Generate" }));
    const shown = await screen.findByRole("dialog", { name: "Your new recovery codes" });
    expect(within(shown).getByRole("list", { name: "recovery codes" })).toHaveTextContent("1111-2222-3333-4444");
    // Escape/close does nothing until the codes are acknowledged.
    await user.click(within(shown).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog", { name: "Your new recovery codes" })).toBeInTheDocument();
    expect(within(shown).getByText(/Confirm that you stored it/)).toBeInTheDocument();
    await user.click(within(shown).getByLabelText(/I have stored this somewhere safe/));
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    expect(await screen.findByText("10 unused")).toBeInTheDocument();
  });

  it("lists sessions and signs out another one or all others", async () => {
    const { api } = securityApi({
      "DELETE /api/v1/auth/sessions/:id": () => noContent(),
      "DELETE /api/v1/auth/sessions": () => json({ revoked: 1 }),
    });
    renderApp("/settings/security");
    const user = userEvent.setup();
    expect(await screen.findByRole("rowheader", { name: /Firefox on Linux This device/ })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Safari on iOS" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out Safari on iOS" }));
    expect(await screen.findByText("Session signed out")).toBeInTheDocument();
    const [revoke] = api.callsTo("DELETE", "/api/v1/auth/sessions/:id");
    expect(revoke?.path).toBe("/api/v1/auth/sessions/s-2");
    expect(revoke?.headers.get("X-CSRF-Token")).toBe("session-token");

    await user.click(screen.getByRole("button", { name: "Sign out all other sessions" }));
    expect(await screen.findByText("1 other sessions signed out")).toBeInTheDocument();
  });

  it("lists tokens with their narrowed scopes and revokes one", async () => {
    const { api } = securityApi({ "DELETE /api/v1/auth/tokens/:id": () => noContent() });
    renderApp("/settings/security");
    const user = userEvent.setup();
    expect(await screen.findByRole("rowheader", { name: "Home Assistant" })).toBeInTheDocument();
    expect(screen.getByText("pfn_0123456789ab_…")).toBeInTheDocument();
    expect(screen.getByText(/narrowed by your current role/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revoke Home Assistant" }));
    expect(await screen.findByText("Token Home Assistant revoked")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/auth/tokens/:id")[0]?.path).toBe("/api/v1/auth/tokens/tok-1");
  });

  it("validates a new token before asking the server", async () => {
    const { api } = securityApi();
    renderApp("/settings/security");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a personal access token" });
    await user.clear(within(form).getByLabelText(/^Expires after/));
    await user.type(within(form).getByLabelText(/^Expires after/), "400");
    await user.click(within(form).getByRole("button", { name: "Create token" }));
    expect(within(form).getByText(/Give the token a name/)).toBeInTheDocument();
    expect(within(form).getByText("Choose 1 to 90 days.")).toBeInTheDocument();
    expect(within(form).getByText("Choose at least one scope.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/tokens")).toHaveLength(0);
  });
});
