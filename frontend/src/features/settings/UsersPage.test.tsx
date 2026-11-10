// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, currentHref, json, mockApi, problem, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import type { AdminUser } from "./usersApi";
import { isLocked, newUserProblems, signInSummary } from "./UsersPage";

const STAMP = "2026-10-01T10:00:00Z";
const PASSWORD = "correct-horse-battery-9";

const account = (id: string, name: string, extra: Partial<AdminUser> = {}): AdminUser => ({
  id,
  username: name.toLowerCase(),
  display_name: name,
  email: null,
  email_verified: false,
  is_active: true,
  is_instance_admin: false,
  is_breakglass: false,
  has_password: true,
  mfa_methods: [],
  locked_until: null,
  last_login_at: null,
  created_at: STAMP,
  ...extra,
});

const ALICE = account("u-1", "Alice", { is_instance_admin: true, mfa_methods: ["totp"], last_login_at: STAMP });
const USERS = [
  ALICE,
  account("u-2", "Bob", { email: "bob@example.org", mfa_methods: ["totp", "webauthn"] }),
  account("u-3", "Carol", { is_active: false, has_password: false }),
];

const ADMIN: SessionOptions = { userId: "u-1", instanceAdmin: true };

/** The admin API behind step-up: changes answer `403 step-up-required` until the factor was verified. */
function usersApi(session: SessionOptions, extra: Record<string, Handler> = {}) {
  const state = { steppedUp: false, users: [...USERS] };
  const guarded =
    (handler: Handler): Handler =>
    (call) =>
      state.steppedUp ? handler(call) : problem(403, "step-up-required");
  const patch = (id: string, change: Partial<AdminUser>): AdminUser => {
    state.users = state.users.map((user) => (user.id === id ? { ...user, ...change } : user));
    return state.users.find((user) => user.id === id) ?? ALICE;
  };
  const routes: Record<string, Handler> = {
    "GET /api/v1/auth/session": (call) => authedSession({ ...session, csrfToken: state.steppedUp ? "stepped-token" : "session-token" })(call),
    "GET /api/v1/households": () => json([]),
    "GET /api/v1/auth/mfa": () =>
      json({ methods: [{ id: "t-1", type: "totp", name: "Phone", created_at: STAMP, last_used_at: null }], recovery_codes_remaining: 10, required: true }),
    "POST /api/v1/auth/step-up": () => {
      state.steppedUp = true;
      return json({ method: "totp", mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "stepped-token" });
    },
    "GET /api/v1/admin/users": () => json({ items: state.users, next_cursor: null }),
    "GET /api/v1/admin/users/:id": (call) => json(state.users.find((user) => call.path.endsWith(`/${user.id}`)) ?? ALICE),
    "POST /api/v1/admin/users": guarded((call) => {
      const body = call.body as { username: string; display_name: string; email?: string };
      const created = account("u-9", body.display_name, { username: body.username, email: body.email ?? null });
      state.users = [...state.users, created];
      return json(created, 201);
    }),
    "PATCH /api/v1/admin/users/:id": guarded((call) => json(patch(call.path.split("/").at(-1) ?? "", call.body as Partial<AdminUser>))),
    "POST /api/v1/admin/users/:id/mfa-reset": guarded((call) => {
      patch(call.path.split("/").at(-2) ?? "", { mfa_methods: [] });
      return json({ factors_removed: 12 });
    }),
  };
  const api = mockApi({ ...routes, ...Object.fromEntries(Object.entries(extra).map(([key, handler]) => [key, guarded(handler)])) });
  // Handlers given in `extra` take the place of the defaults.
  for (const [key, handler] of Object.entries(extra)) {
    api.on(key, guarded(handler));
  }
  return { api, state };
}

async function stepUp(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), "246810");
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
}

async function fillNewUser(user: ReturnType<typeof userEvent.setup>, form: HTMLElement, username: string, name: string, password = PASSWORD) {
  await user.clear(within(form).getByLabelText(/^Username/));
  await user.type(within(form).getByLabelText(/^Username/), username);
  await user.clear(within(form).getByLabelText(/^Name/));
  await user.type(within(form).getByLabelText(/^Name/), name);
  await user.clear(within(form).getByLabelText(/^Initial password/));
  await user.type(within(form).getByLabelText(/^Initial password/), password);
  await user.clear(within(form).getByLabelText(/^Repeat the password/));
  await user.type(within(form).getByLabelText(/^Repeat the password/), password);
}

describe("user rules", () => {
  it("checks a new account the way the server does", () => {
    const form = { username: "Dana", displayName: "Dana", email: "", password: PASSWORD, confirm: PASSWORD };
    expect(newUserProblems(form)).toEqual({});
    expect(newUserProblems({ ...form, username: "" })["username"]).toBe("Enter a username.");
    expect(newUserProblems({ ...form, username: "dana smith" })["username"]).toMatch(/lower-case letters/);
    expect(newUserProblems({ ...form, username: "-dana" })["username"]).toMatch(/starting with a letter or digit/);
    expect(newUserProblems({ ...form, displayName: " " })["display_name"]).toBe("Enter the person's name.");
    expect(newUserProblems({ ...form, password: "short", confirm: "short" })["password"]).toBe("Use at least 12 characters.");
    expect(newUserProblems({ ...form, confirm: `${PASSWORD}x` })["confirm"]).toBe("The passwords do not match.");
    expect(newUserProblems({ ...form, username: "dana-dana-dana", password: "Dana-Dana-Dana", confirm: "Dana-Dana-Dana" })["password"]).toBe(
      "The password must not be the username.",
    );
  });

  it("describes how an account signs in and whether it is locked", () => {
    expect(signInSummary({ has_password: true, mfa_methods: [] })).toBe("Password, no second factor");
    expect(signInSummary({ has_password: true, mfa_methods: ["totp", "webauthn", "webauthn"] })).toBe("Password, authenticator app and security key");
    expect(signInSummary({ has_password: false, mfa_methods: [] })).toBe("No password (single sign-on only), no second factor");
    const now = new Date("2026-11-03T10:00:00Z");
    expect(isLocked({ locked_until: "2026-11-03T10:05:00Z" }, now)).toBe(true);
    expect(isLocked({ locked_until: "2026-11-03T09:55:00Z" }, now)).toBe(false);
    expect(isLocked({ locked_until: null }, now)).toBe(false);
  });
});

describe("users page access", () => {
  it("is not listed for, and tells nothing to, someone who is not an instance administrator", async () => {
    const { api } = usersApi({ userId: "u-2", role: "owner", instanceAdmin: false });
    const view = renderApp("/settings");
    expect(await screen.findByRole("link", { name: "Household members" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    view.unmount();

    renderApp("/settings/users");
    expect(await screen.findByText(/Only an administrator of this PerFin instance can manage users/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Create a user" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(api.calls.some((call) => call.path.startsWith("/api/v1/admin/"))).toBe(false);
  });

  it("is listed for an instance administrator and shows every account", async () => {
    usersApi(ADMIN);
    renderApp("/settings");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Users" }));
    const table = await screen.findByRole("region", { name: "Users of this PerFin instance" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Alice (alice)");
    expect(rows[0]).toHaveTextContent("You");
    expect(rows[0]).toHaveTextContent("Instance admin");
    expect(rows[0]).toHaveTextContent("Password, authenticator app");
    expect(rows[1]).toHaveTextContent("bob@example.org (not checked)");
    expect(rows[2]).toHaveTextContent("Disabled");
    expect(rows[2]).toHaveTextContent("No password (single sign-on only), no second factor");
    // Nobody changes their own account here.
    expect(within(within(table).getByRole("row", { name: /Alice/ })).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable Bob" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable Carol" })).toBeInTheDocument();
    // The admin has a factor, so there is no "set one up first" note.
    expect(screen.queryByText(/your account has none yet/)).not.toBeInTheDocument();
  });

  it("warns an administrator without a second factor that changes will be refused", async () => {
    usersApi({ ...ADMIN, enrolled: false });
    renderApp("/settings/users");
    expect(await screen.findByRole("note")).toHaveTextContent(/asks for your second factor, and your account has none yet/);
  });
});

describe("creating a user", () => {
  it("creates a local account after step-up and says what to do next", async () => {
    const { api } = usersApi(ADMIN);
    const view = renderApp("/settings/users");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a user" });
    expect(within(form).getByText(/12 to 128 characters, not the username, and not a password known from a data breach/)).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    expect(await within(form).findByText("Enter a username.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(0);

    await fillNewUser(user, form, " Dana ", "Dana Smith");
    await user.type(within(form).getByLabelText(/^Email/), "dana@example.org");
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    await stepUp(user);

    const note = await screen.findByRole("status");
    expect(note).toHaveTextContent("The account dana exists and signs in with the password you set.");
    expect(note).toHaveTextContent(/It is not in any household yet/);
    expect(note).toHaveTextContent(/Choose “Someone with a PerFin account”, enter the username dana/);
    const calls = api.callsTo("POST", "/api/v1/admin/users");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body).toEqual({ username: "dana", display_name: "Dana Smith", password: PASSWORD, email: "dana@example.org" });
    expect(calls[1]?.headers.get("X-CSRF-Token")).toBe("stepped-token");
    expect(await screen.findByRole("rowheader", { name: /Dana Smith/ })).toBeInTheDocument();
    // The form is empty again: the password does not linger on the page.
    expect(within(form).getByLabelText(/^Initial password/)).toHaveValue("");

    await user.click(within(note).getByRole("link", { name: "Household members" }));
    await waitFor(() => {
      expect(currentHref(view.router)).toBe("/settings/household");
    });
  });

  it("shows the server's refusals at the field they are about", async () => {
    let refusal = problem(409, "username-taken");
    const { api, state } = usersApi(ADMIN, { "POST /api/v1/admin/users": () => refusal });
    state.steppedUp = true;
    renderApp("/settings/users");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a user" });
    await fillNewUser(user, form, "bob", "Bob Two");
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    expect(await within(form).findByText("That username is already in use. Choose another one.")).toBeInTheDocument();

    refusal = problem(422, "weak-password", { reason: "breached" });
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    expect(await within(form).findByText("This password appears in known data breaches. Choose another one.")).toBeInTheDocument();

    refusal = problem(403, "instance-admin-required");
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("detail of instance-admin-required");
    expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(3);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // What was typed is kept for another try.
    expect(within(form).getByLabelText(/^Username/)).toHaveValue("bob");
  });

  it("does nothing when the step-up is cancelled", async () => {
    const { api } = usersApi(ADMIN);
    renderApp("/settings/users");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Create a user" });
    await fillNewUser(user, form, "dana", "Dana");
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(within(form).getByRole("button", { name: "Create user" })).toBeEnabled();
    });
    expect(api.callsTo("POST", "/api/v1/admin/users")).toHaveLength(1);
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("changing a user", () => {
  it("disables an account after a confirmation and step-up, and enables it again", async () => {
    const { api } = usersApi(ADMIN);
    renderApp("/settings/users");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Disable Bob" }));
    const dialog = await screen.findByRole("dialog", { name: "Disable Bob?" });
    expect(dialog).toHaveTextContent(/signed out everywhere at once and their personal access tokens are revoked for good/);
    expect(api.callsTo("PATCH", "/api/v1/admin/users/:id")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Disable account" }));
    await stepUp(user);
    expect(await screen.findByText("Bob disabled")).toBeInTheDocument();
    const calls = api.callsTo("PATCH", "/api/v1/admin/users/:id");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.path).toBe("/api/v1/admin/users/u-2");
    expect(calls[1]?.body).toEqual({ is_active: false });
    expect(calls[1]?.headers.get("X-CSRF-Token")).toBe("stepped-token");

    await user.click(await screen.findByRole("button", { name: "Enable Bob" }));
    expect(await screen.findByText("Bob can sign in again")).toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/admin/users/:id").at(-1)?.body).toEqual({ is_active: true });
    expect(await screen.findByRole("button", { name: "Disable Bob" })).toBeInTheDocument();
  });

  it("edits only the flags the server accepts and sends only what changed", async () => {
    const { api, state } = usersApi(ADMIN);
    state.steppedUp = true;
    renderApp("/settings/users");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit Bob" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Bob" });
    const verified = await within(dialog).findByRole("checkbox", { name: "Email address checked" });
    const breakglass = within(dialog).getByRole("checkbox", { name: "Break-glass account" });
    expect(verified).not.toBeChecked();
    expect(breakglass).not.toBeChecked();
    expect(dialog).toHaveTextContent(/You vouch that bob@example.org belongs to them/);
    expect(dialog).toHaveTextContent(/instance administrator cannot be changed here/);
    expect(api.callsTo("GET", "/api/v1/admin/users/u-2")).toHaveLength(1);
    await user.click(verified);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Bob saved")).toBeInTheDocument();
    const calls = api.callsTo("PATCH", "/api/v1/admin/users/:id");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toEqual({ email_verified: true });
    expect(await screen.findByText("bob@example.org", { exact: false })).toHaveTextContent("(checked)");
  });

  it("does not offer what the server would refuse, and shows a refusal it could not foresee", async () => {
    const { api, state } = usersApi(ADMIN, {
      "PATCH /api/v1/admin/users/:id": () =>
        json({ type: "urn:perfin:problem:no-local-password", title: "No local password", status: 409, detail: "A break-glass account needs a local password." }, 409),
    });
    state.steppedUp = true;
    renderApp("/settings/users");
    const user = userEvent.setup();
    // Carol has no password and no email: neither box can be ticked.
    await user.click(await screen.findByRole("button", { name: "Edit Carol" }));
    let dialog = await screen.findByRole("dialog", { name: "Edit Carol" });
    expect(await within(dialog).findByRole("checkbox", { name: "Break-glass account" })).toBeDisabled();
    expect(within(dialog).getByRole("checkbox", { name: "Email address checked" })).toBeDisabled();
    expect(dialog).toHaveTextContent("A break-glass account needs a local password, and this account has none.");
    // Saving without a change sends nothing.
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Edit Carol" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("PATCH", "/api/v1/admin/users/:id")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Edit Bob" }));
    dialog = await screen.findByRole("dialog", { name: "Edit Bob" });
    await user.click(await within(dialog).findByRole("checkbox", { name: "Break-glass account" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A break-glass account needs a local password.");
    expect(api.callsTo("PATCH", "/api/v1/admin/users/:id")[0]?.body).toEqual({ is_breakglass: true });
  });

  it("resets a user's second factors only after a confirmation and step-up", async () => {
    const { api } = usersApi(ADMIN);
    renderApp("/settings/users");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reset second factors of Bob" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset the second factors of Bob?" });
    expect(dialog).toHaveTextContent(/Every authenticator app, security key and recovery code of the account is removed/);
    expect(dialog).toHaveTextContent(/Their password stays/);
    expect(api.callsTo("POST", "/api/v1/admin/users/:id/mfa-reset")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Reset second factors" }));
    await stepUp(user);
    expect(await screen.findByText("Second factors of Bob reset")).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/admin/users/:id/mfa-reset");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.path).toBe("/api/v1/admin/users/u-2/mfa-reset");
    expect(calls[1]?.headers.get("X-CSRF-Token")).toBe("stepped-token");
    const table = screen.getByRole("region", { name: "Users of this PerFin instance" });
    await waitFor(() => {
      expect(within(table).getAllByRole("row")[2]).toHaveTextContent("Password, no second factor");
    });
  });

  it("leaves the account alone when the confirmation is cancelled", async () => {
    const { api } = usersApi(ADMIN);
    renderApp("/settings/users");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reset second factors of Bob" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset the second factors of Bob?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.calls.some((call) => call.method !== "GET")).toBe(false);
  });
});
