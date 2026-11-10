// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  authedSession,
  currentHref,
  json,
  mockApi,
  noContent,
  problem,
  renderApp,
  type Handler,
} from "../auth/testing";
import { account, page } from "../transactions/fixtures";

const EVERYDAY = account("a-1", "Everyday", {
  balance: { amount: "1200.50", currency: "USD" },
});

function detail(
  role: "owner" | "admin" | "member" | "viewer",
  extra: Record<string, Handler> = {},
  shown = EVERYDAY,
) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    "GET /api/v1/accounts": page([shown]),
    "GET /api/v1/accounts/:id": () => json(shown),
    "GET /api/v1/accounts/:id/balances": () =>
      json({
        account_id: shown.id,
        currency: "USD",
        start: "2025-11-01",
        end: "2026-11-03",
        items: [],
        snapshots: [],
      }),
    ...extra,
  });
}

describe("delete account", () => {
  it("deletes only once the name is typed, then returns to the accounts list", async () => {
    const api = detail("owner", {
      "DELETE /api/v1/accounts/:id": () => noContent(),
    });
    const { router } = renderApp("/accounts/a-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Delete account" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Everyday?",
    });
    expect(dialog).toHaveTextContent(
      "permanently deletes the account and everything recorded on it",
    );
    expect(dialog).toHaveTextContent("every transaction on it");
    expect(dialog).toHaveTextContent("Close it instead");
    const confirm = within(dialog).getByRole("button", {
      name: "Delete account",
    });
    expect(confirm).toBeDisabled();
    const field = within(dialog).getByLabelText(/^Type the account name/);
    await user.type(field, "everyday");
    expect(confirm).toBeDisabled();
    expect(api.callsTo("DELETE", "/api/v1/accounts/:id")).toHaveLength(0);
    await user.clear(field);
    await user.type(field, "Everyday");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(currentHref(router)).toBe("/accounts");
    });
    const calls = api.callsTo("DELETE", "/api/v1/accounts/a-1");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Everyday deleted")).toBeInTheDocument();
  });

  it("shows the server's refusal and stays on the account", async () => {
    const api = detail("admin", {
      "DELETE /api/v1/accounts/:id": () => problem(403, "forbidden"),
    });
    const { router } = renderApp("/accounts/a-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Delete account" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Everyday?",
    });
    await user.type(
      within(dialog).getByLabelText(/^Type the account name/),
      "Everyday",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Delete account" }),
    );
    expect(
      await within(dialog).findByText("detail of forbidden"),
    ).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/accounts/a-1")).toHaveLength(1);
    expect(currentHref(router)).toBe("/accounts/a-1");
  });

  it("offers closing instead and hands over to the close dialog", async () => {
    detail("owner");
    renderApp("/accounts/a-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Delete account" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Everyday?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Close it instead" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Close Everyday?" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "Delete Everyday?" }),
    ).not.toBeInTheDocument();
  });

  it("does not suggest closing an account that is already closed", async () => {
    detail(
      "owner",
      {},
      account("a-1", "Everyday", { is_closed: true, closed_on: "2026-01-01" }),
    );
    renderApp("/accounts/a-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Delete account" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Everyday?",
    });
    expect(dialog).toHaveTextContent("This account is already closed");
    expect(
      within(dialog).queryByRole("button", { name: "Close it instead" }),
    ).not.toBeInTheDocument();
  });

  it("is offered to owners and admins only", async () => {
    detail("member");
    const { unmount } = renderApp("/accounts/a-1");
    expect(
      await screen.findByRole("button", { name: "Close account" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete account" }),
    ).not.toBeInTheDocument();
    unmount();

    detail("viewer");
    renderApp("/accounts/a-1");
    await screen.findByRole("heading", { name: "Everyday" });
    expect(
      screen.queryByRole("button", { name: "Delete account" }),
    ).not.toBeInTheDocument();
  });
});
