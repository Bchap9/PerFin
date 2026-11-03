// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ANON_SESSION, authedSession, currentHref, json, mockApi, pendingSession, problem, renderApp, type Handler } from "./testing";

const OPTIONS: Handler = () => json({ local_login: true, password_reset: true });
const OIDC_ON: Handler = () => json({ enabled: true });
const OIDC_OFF: Handler = () => json({ enabled: false });
const SETUP_DONE: Handler = () => json({ setup_required: false });

function loginApi(extra: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/v1/auth/session": ANON_SESSION,
    "GET /api/v1/auth/login/options": OPTIONS,
    "GET /api/v1/auth/oidc/status": OIDC_OFF,
    "GET /api/v1/setup/status": SETUP_DONE,
    ...extra,
  });
}

async function fillAndSubmit(username: string, password: string) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/^Username/), username);
  await user.type(screen.getByLabelText(/^Password/), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("login page", () => {
  it("signs in with the pre-auth CSRF token and continues to return_to", async () => {
    let signedIn = false;
    const api = loginApi({
      "GET /api/v1/auth/session": (call) => (signedIn ? authedSession()(call) : ANON_SESSION(call)),
      "POST /api/v1/auth/login": () => {
        signedIn = true;
        return json({ status: "authenticated", csrf_token: "session-token", mfa: { required: false, methods: [] } });
      },
    });
    const { router } = renderApp("/settings/profile");
    // The guard sent us to sign-in, remembering the page.
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(currentHref(router)).toBe("/login?return_to=%2Fsettings%2Fprofile");

    await fillAndSubmit("alice", "correct horse battery");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings/profile");
    });
    const [loginCall] = api.callsTo("POST", "/api/v1/auth/login");
    expect(loginCall?.headers.get("X-CSRF-Token")).toBe("pre-token");
    expect(loginCall?.body).toEqual({ username: "alice", password: "correct horse battery", remember_device: false });
    expect(await screen.findByRole("heading", { level: 1, name: "Profile" })).toBeInTheDocument();
  });

  it("continues to the second-factor step when MFA is required", async () => {
    let pending = false;
    loginApi({
      "GET /api/v1/auth/session": (call) => (pending ? pendingSession(["totp"])(call) : ANON_SESSION(call)),
      "POST /api/v1/auth/login": () => {
        pending = true;
        return json({ status: "mfa_required", csrf_token: "pending-token", mfa: { required: true, methods: ["totp"] } });
      },
    });
    const { router } = renderApp("/login?return_to=%2Fbudgets");
    await fillAndSubmit("alice", "correct horse battery");
    await waitFor(() => {
      expect(currentHref(router)).toBe("/login/mfa?return_to=%2Fbudgets");
    });
    expect(await screen.findByLabelText(/^Authentication code/)).toBeInTheDocument();
  });

  it("announces a refused login, clears the password and focuses it", async () => {
    loginApi({ "POST /api/v1/auth/login": () => problem(401, "invalid-credentials") });
    const { router } = renderApp("/login");
    await fillAndSubmit("alice", "wrong password!!");
    expect(await screen.findByText("The username or password is not correct.")).toHaveAttribute("role", "alert");
    const password = screen.getByLabelText(/^Password/);
    expect(password).toHaveValue("");
    expect(password).toHaveFocus();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("validates empty fields without calling the server", async () => {
    const api = loginApi();
    renderApp("/login");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter your username.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Username/)).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", "/api/v1/auth/login")).toHaveLength(0);
  });

  it("never follows an off-site return_to after login", async () => {
    let signedIn = false;
    loginApi({
      "GET /api/v1/auth/session": (call) => (signedIn ? authedSession()(call) : ANON_SESSION(call)),
      "POST /api/v1/auth/login": () => {
        signedIn = true;
        return json({ status: "authenticated", csrf_token: "session-token", mfa: { required: false, methods: [] } });
      },
    });
    const { router } = renderApp("/login?return_to=%2F%2Fevil.example%2Fphish");
    await fillAndSubmit("alice", "correct horse battery");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
  });

  it("offers single sign-on, password reset and shows OIDC errors", async () => {
    loginApi({ "GET /api/v1/auth/oidc/status": OIDC_ON });
    renderApp("/login?return_to=%2Faccounts&oidc_error=not-in-group");
    const sso = await screen.findByRole("link", { name: "Sign in with Authentik" });
    expect(sso).toHaveAttribute("href", "/api/v1/auth/oidc/start?return_to=%2Faccounts");
    expect(screen.getByRole("link", { name: "Forgot your password?" })).toHaveAttribute("href", "/forgot-password");
    expect(screen.getByText(/not allowed to use PerFin/)).toHaveAttribute("role", "alert");
  });

  it("hides the password form when local login is off for this network", async () => {
    loginApi({
      "GET /api/v1/auth/login/options": () => json({ local_login: false, password_reset: false }),
      "GET /api/v1/auth/oidc/status": OIDC_ON,
    });
    renderApp("/login");
    expect(await screen.findByRole("link", { name: "Sign in with Authentik" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Password/)).not.toBeInTheDocument();
  });

  it("points to the setup link while first-run setup is pending", async () => {
    loginApi({ "GET /api/v1/setup/status": () => json({ setup_required: true }) });
    renderApp("/login");
    expect(await screen.findByText(/PerFin is not set up yet/)).toBeInTheDocument();
  });

  it("moves a signed-in user straight on", async () => {
    loginApi({ "GET /api/v1/auth/session": authedSession() });
    const { router } = renderApp("/login?return_to=%2Fsettings");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings");
    });
  });
});
