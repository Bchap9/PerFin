// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, noContent, problem, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import type { Member } from "./api";
import { assignableRoles, canEditMember } from "./HouseholdMembersPage";

const member = (userId: string, name: string, role: Member["role"], extra: Partial<Member> = {}): Member => ({
  user_id: userId,
  username: name.toLowerCase(),
  display_name: name,
  role,
  is_active: true,
  joined_at: "2026-01-01T00:00:00Z",
  ...extra,
});

const MEMBERS = [
  member("u-1", "Alice", "owner"),
  member("u-2", "Bob", "admin"),
  member("u-3", "Carol", "member"),
  member("u-4", "Dave", "viewer", { is_active: false }),
];

function membersApi(session: SessionOptions, extra: Record<string, Handler> = {}) {
  const state = { steppedUp: false, members: [...MEMBERS] };
  const api = mockApi({
    "GET /api/v1/auth/session": (call) => authedSession({ ...session, csrfToken: state.steppedUp ? "stepped-token" : "session-token" })(call),
    "GET /api/v1/households": () => json([]),
    "GET /api/v1/households/:id/members": () => json(state.members),
    "GET /api/v1/auth/mfa": () => json({ methods: [{ id: "t-1", type: "totp", name: "Phone", created_at: "2026-10-01T10:00:00Z", last_used_at: null }], recovery_codes_remaining: 10, required: true }),
    "POST /api/v1/auth/step-up": () => {
      state.steppedUp = true;
      return json({ method: "totp", mfa_verified_at: "2026-11-03T10:00:00Z", csrf_token: "stepped-token" });
    },
    ...extra,
  });
  return { api, state };
}

async function stepUp(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
  await user.type(await within(dialog).findByLabelText(/^Authentication code/), "246810");
  await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
}

describe("member rules", () => {
  it("offers only what the server would allow", () => {
    expect(assignableRoles("owner")).toEqual(["owner", "admin", "member", "viewer"]);
    expect(assignableRoles("admin")).toEqual(["admin", "member", "viewer"]);
    expect(assignableRoles("member")).toEqual([]);
    const owner = member("u-9", "Olga", "owner");
    expect(canEditMember("owner", "u-1", owner)).toBe(true);
    expect(canEditMember("admin", "u-2", owner)).toBe(false);
    expect(canEditMember("admin", "u-2", member("u-3", "Carol", "member"))).toBe(true);
    expect(canEditMember("owner", "u-1", member("u-1", "Alice", "owner"))).toBe(false);
    expect(canEditMember("viewer", "u-4", member("u-3", "Carol", "member"))).toBe(false);
  });
});

describe("household members page", () => {
  it("lets an owner change a role after step-up", async () => {
    const { api, state } = membersApi({ userId: "u-1", role: "owner" }, {
      "PATCH /api/v1/households/:id/members/:user": () => {
        if (!state.steppedUp) {
          return problem(403, "step-up-required");
        }
        return json(member("u-3", "Carol", "admin"));
      },
    });
    renderApp("/settings/household");
    const user = userEvent.setup();
    const carol = await screen.findByLabelText("Role of Carol");
    // Nobody edits their own membership.
    expect(screen.queryByLabelText("Role of Alice")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove Alice" })).not.toBeInTheDocument();
    await user.selectOptions(carol, "admin");
    await stepUp(user);
    expect(await screen.findByText("Carol is now admin")).toBeInTheDocument();
    const calls = api.callsTo("PATCH", "/api/v1/households/:id/members/:user");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.path).toBe("/api/v1/households/h-1/members/u-3");
    expect(calls[1]?.body).toEqual({ role: "admin" });
    expect(calls[1]?.headers.get("X-CSRF-Token")).toBe("stepped-token");
  });

  it("does not let an admin touch the owner or appoint owners", async () => {
    membersApi({ userId: "u-2", role: "admin" });
    renderApp("/settings/household");
    const carol = await screen.findByLabelText("Role of Carol");
    expect(within(carol).queryByRole("option", { name: "Owner" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Role of Alice")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove Alice" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove Carol" })).toBeInTheDocument();
  });

  it("shows a read-only list to viewers", async () => {
    const { api } = membersApi({ userId: "u-4", role: "viewer" });
    renderApp("/settings/household");
    expect(await screen.findByRole("rowheader", { name: /Carol/ })).toBeInTheDocument();
    expect(screen.getByText(/Only owners and admins can change members/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add a member" })).not.toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(api.calls.some((call) => call.method !== "GET")).toBe(false);
  });

  it("adds an existing user and creates a new account", async () => {
    const { api, state } = membersApi({ userId: "u-1", role: "owner" }, {
      "POST /api/v1/households/:id/members": (call) => {
        const body = call.body as { username?: string; new_user?: { display_name: string } };
        const name = body.username === undefined ? (body.new_user?.display_name ?? "?") : "Erin";
        const added = member(`u-${name}`, name, "member");
        state.members = [...state.members, added];
        return json(added, 201);
      },
    });
    state.steppedUp = true;
    renderApp("/settings/household");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Add a member" });
    await user.type(within(form).getByLabelText(/^Username/), "Erin");
    await user.click(within(form).getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("Erin added as member")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/households/:id/members")[0]?.body).toEqual({ role: "member", username: "erin" });

    await user.click(within(form).getByLabelText("Create a new account"));
    await user.type(within(form).getByLabelText(/^Username/), "frank");
    await user.type(within(form).getByLabelText(/^Name/), "Frank");
    await user.type(within(form).getByLabelText(/^Initial password/), "frank's long passphrase");
    await user.type(within(form).getByLabelText(/^Repeat the password/), "frank's long passphrase");
    await user.selectOptions(within(form).getByLabelText(/^Role/), "viewer");
    await user.click(within(form).getByRole("button", { name: "Add member" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/households/:id/members")).toHaveLength(2);
    });
    expect(api.callsTo("POST", "/api/v1/households/:id/members")[1]?.body).toEqual({
      role: "viewer",
      new_user: { username: "frank", display_name: "Frank", password: "frank's long passphrase" },
    });
    expect(await screen.findByRole("rowheader", { name: /Frank/ })).toBeInTheDocument();
  });

  it("shows the server's refusal when adding fails", async () => {
    const { state } = membersApi({ userId: "u-1", role: "owner" }, {
      "POST /api/v1/households/:id/members": () => problem(404, "user-not-found", { detail: "User not found" }),
    });
    state.steppedUp = true;
    renderApp("/settings/household");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Add a member" });
    await user.type(within(form).getByLabelText(/^Username/), "nobody");
    await user.click(within(form).getByRole("button", { name: "Add member" }));
    expect(await within(form).findByText("User not found")).toHaveAttribute("role", "alert");
  });

  it("removes a member after confirmation", async () => {
    const { api, state } = membersApi({ userId: "u-1", role: "owner" }, {
      "DELETE /api/v1/households/:id/members/:user": () => {
        state.members = state.members.filter((each) => each.user_id !== "u-3");
        return noContent();
      },
    });
    state.steppedUp = true;
    renderApp("/settings/household");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove Carol" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove Carol?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("Carol removed")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("rowheader", { name: /Carol/ })).not.toBeInTheDocument();
    });
    expect(api.callsTo("DELETE", "/api/v1/households/:id/members/:user")[0]?.headers.get("X-CSRF-Token")).toBe("stepped-token");
  });

  it("explains when the user has no household", async () => {
    membersApi({ household: false });
    renderApp("/settings/household");
    expect(await screen.findByText(/not a member of a household yet/)).toBeInTheDocument();
  });
});
