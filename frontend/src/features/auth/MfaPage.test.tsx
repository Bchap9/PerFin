// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ANON_SESSION, authedSession, currentHref, json, mockApi, noContent, pendingSession, problem, renderApp, type Handler } from "./testing";

const webauthn = vi.hoisted(() => ({
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
  supported: { value: true },
}));

vi.mock("@simplewebauthn/browser", () => ({
  browserSupportsWebAuthn: () => webauthn.supported.value,
  startAuthentication: webauthn.startAuthentication,
  startRegistration: webauthn.startRegistration,
}));

const VERIFIED = (method: string, extra: Record<string, unknown> = {}): Response =>
  json({ method, mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "full-token", ...extra });

/** A pending session that becomes a full one once verification succeeds. */
function pendingThenFull(methods: string[], verify: Handler) {
  let verified = false;
  return mockApi({
    "GET /api/v1/auth/session": (call) => (verified ? authedSession()(call) : pendingSession(methods)(call)),
    "POST /api/v1/auth/mfa/verify": async (call) => {
      const response = await verify(call);
      verified = response.ok;
      return response;
    },
    "POST /api/v1/auth/mfa/webauthn/authentication-options": () =>
      json({ challenge: "Y2hhbGxlbmdl", rpId: "localhost", allowCredentials: [], timeout: 60000 }),
    "POST /api/v1/auth/logout": () => noContent(),
  });
}

describe("second-factor step", () => {
  beforeEach(() => {
    webauthn.supported.value = true;
    webauthn.startAuthentication.mockReset();
  });

  it("verifies a TOTP code with the pending session's CSRF token, then continues", async () => {
    const api = pendingThenFull(["totp"], () => VERIFIED("totp"));
    const { router } = renderApp("/accounts");
    // The guard sends a pending login to the second-factor step.
    await waitFor(() => {
      expect(currentHref(router)).toBe("/login/mfa?return_to=%2Faccounts");
    });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Authentication code/), "123 456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/accounts");
    });
    const [call] = api.callsTo("POST", "/api/v1/auth/mfa/verify");
    expect(call?.body).toEqual({ method: "totp", code: "123456" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("pending-token");
  });

  it("uses a security key through WebAuthn", async () => {
    webauthn.startAuthentication.mockResolvedValue({ id: "cred-1", rawId: "cred-1", type: "public-key", response: {} });
    const api = pendingThenFull(["webauthn", "totp"], () => VERIFIED("webauthn"));
    const { router } = renderApp("/login/mfa?return_to=%2Fbudgets");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Use security key or passkey" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/budgets");
    });
    expect(webauthn.startAuthentication).toHaveBeenCalledWith({
      optionsJSON: expect.objectContaining({ challenge: "Y2hhbGxlbmdl" }) as unknown,
    });
    expect(api.callsTo("POST", "/api/v1/auth/mfa/webauthn/authentication-options")[0]?.headers.get("X-CSRF-Token")).toBe(
      "pending-token",
    );
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")[0]?.body).toEqual({
      method: "webauthn",
      credential: { id: "cred-1", rawId: "cred-1", type: "public-key", response: {} },
    });
  });

  it("explains a cancelled security key prompt", async () => {
    webauthn.startAuthentication.mockRejectedValue(Object.assign(new Error("no"), { name: "NotAllowedError" }));
    const api = pendingThenFull(["webauthn"], () => VERIFIED("webauthn"));
    renderApp("/login/mfa");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Use security key or passkey" }));
    expect(await screen.findByText(/cancelled or timed out/)).toHaveAttribute("role", "alert");
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")).toHaveLength(0);
  });

  it("accepts a recovery code and warns when few are left", async () => {
    const api = pendingThenFull(["totp"], () => VERIFIED("recovery", { recovery_codes_remaining: 1 }));
    const { router } = renderApp("/login/mfa");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Use a recovery code" }));
    await user.type(screen.getByLabelText(/^Recovery code/), "ABCD-EFGH-JKLM-NPQR");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")[0]?.body).toEqual({
      method: "recovery",
      code: "ABCD-EFGH-JKLM-NPQR",
    });
    expect(await screen.findByText("1 recovery codes left")).toBeInTheDocument();
  });

  it("keeps the step open and announces a rejected code", async () => {
    pendingThenFull(["totp"], () => problem(400, "mfa-invalid"));
    const { router } = renderApp("/login/mfa");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Authentication code/), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("That code or key was not accepted. Try again.")).toHaveAttribute("role", "alert");
    expect(router.state.location.pathname).toBe("/login/mfa");
  });

  it("checks the code format before sending", async () => {
    const api = pendingThenFull(["totp"], () => VERIFIED("totp"));
    renderApp("/login/mfa");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Authentication code/), "12ab");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText(/Enter the 6-digit code/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/mfa/verify")).toHaveLength(0);
  });

  it("can cancel the login", async () => {
    let loggedOut = false;
    const api = mockApi({
      "GET /api/v1/auth/session": (call) => (loggedOut ? ANON_SESSION(call) : pendingSession(["totp"])(call)),
      "POST /api/v1/auth/logout": () => {
        loggedOut = true;
        return noContent();
      },
    });
    const { router } = renderApp("/login/mfa");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Cancel and sign out" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    expect(api.callsTo("POST", "/api/v1/auth/logout")[0]?.headers.get("X-CSRF-Token")).toBe("pending-token");
  });

  it("says when there is no pending login", async () => {
    mockApi({ "GET /api/v1/auth/session": ANON_SESSION });
    renderApp("/login/mfa");
    expect(await screen.findByText(/expired or was already completed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/login");
  });
});
