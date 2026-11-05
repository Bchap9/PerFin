// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { ACCOUNT_CHECKING, ownerSession, referenceHandlers } from "../planning/testing";
import { levelChoices } from "./AccessPage";

const STAMP = "2026-11-03T10:00:00Z";

describe("household settings", () => {
  it("changes the base currency", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "PATCH /api/v1/households/:id": () => json({ id: "h-1", name: "Home", base_currency: "EUR", created_at: STAMP, role: "owner" }),
    });
    renderApp("/settings/general");
    const user = userEvent.setup();
    const currency = await screen.findByLabelText("Base currency");
    expect(currency).toHaveValue("USD");
    await user.clear(currency);
    await user.type(currency, "eu");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/three-letter currency code/)).toBeInTheDocument();
    await user.type(currency, "r");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/households/h-1")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/households/h-1")[0]?.body).toEqual({ name: "Home", base_currency: "EUR" });
  });

  it("is read-only for a member", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession({ role: "member" }), ...referenceHandlers() });
    renderApp("/settings/general");
    expect(await screen.findByLabelText("Base currency")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });
});

describe("account access", () => {
  it("never offers edit to a viewer", () => {
    expect(levelChoices("viewer").map((c) => c.value)).toEqual(["none", "view"]);
    expect(levelChoices("member").map((c) => c.value)).toEqual(["none", "view", "edit"]);
  });

  it("grants and revokes access per member, each with step-up", async () => {
    const state = { stepUps: 0 };
    const handlers: Record<string, Handler> = {
      "GET /api/v1/auth/session": (call) => authedSession({ role: "owner", csrfToken: `t-${String(state.stepUps)}` })(call),
      ...referenceHandlers(),
      "GET /api/v1/households/:id/members": () =>
        json([
          { user_id: "u-1", username: "alice", display_name: "Alice", role: "owner", is_active: true, joined_at: STAMP },
          { user_id: "u-3", username: "carol", display_name: "Carol", role: "member", is_active: true, joined_at: STAMP },
        ]),
      "GET /api/v1/households/:id/members/:user/grants": () => json([]),
      "GET /api/v1/auth/mfa": () => json({ methods: [{ id: "t-1", type: "totp", name: "Phone", created_at: STAMP, last_used_at: null }], recovery_codes_remaining: 10, required: true }),
      "POST /api/v1/auth/step-up": () => {
        state.stepUps += 1;
        return json({ method: "totp", mfa_verified_at: STAMP, csrf_token: `t-${String(state.stepUps)}` });
      },
      "PUT /api/v1/households/:id/members/:user/grants/:account": () =>
        state.stepUps < 1 ? problem(403, "step-up-required") : json({ id: "gr-1", account_id: ACCOUNT_CHECKING, user_id: "u-3", level: "edit", updated_at: STAMP }),
    };
    const api = mockApi(handlers);
    renderApp("/settings/access");
    const user = userEvent.setup();
    const chooser = await screen.findByLabelText("Member");
    // Owners see everything, so only members and viewers are listed.
    expect(within(chooser).queryByText(/Alice/)).not.toBeInTheDocument();
    await user.selectOptions(chooser, "u-3");
    await user.selectOptions(await screen.findByLabelText("Access to Checking"), "edit");
    const dialog = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.type(await within(dialog).findByLabelText(/^Authentication code/), "246810");
    await user.click(within(dialog).getByRole("button", { name: "Verify and continue" }));
    await screen.findByText("Access updated");
    const puts = api.callsTo("PUT", "/api/v1/households/h-1/members/u-3/grants/" + ACCOUNT_CHECKING);
    expect(puts.at(-1)?.body).toEqual({ level: "edit" });
  });
});
