// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";

import { json, mockApi, renderApp } from "../auth/testing";
import { ACCOUNT_CHECKING, ownerSession, referenceHandlers } from "../planning/testing";

function handlers(accounts: unknown[], role: "owner" | "viewer" = "owner") {
  return {
    ...ownerSession({ role }),
    ...referenceHandlers(),
    "GET /api/v1/accounts": () => json({ items: accounts, next_cursor: null }),
  };
}

describe("getting started", () => {
  it("tells a household with no accounts where to begin", async () => {
    mockApi(handlers([]));
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Start here" });
    const steps = within(card).getAllByRole("listitem");
    expect(steps).toHaveLength(5);
    expect(within(card).getByRole("link", { name: "Upload a statement or an export from your bank" })).toHaveAttribute(
      "href",
      "/documents",
    );
    expect(within(card).getByRole("link", { name: "Or add an account by hand" })).toHaveAttribute("href", "/accounts");
    expect(within(card).getByRole("link", { name: "Check the review queue" })).toHaveAttribute("href", "/review");
  });

  it("is gone once there is an account", async () => {
    mockApi(handlers([{ id: ACCOUNT_CHECKING, name: "Everyday", is_closed: false }]));
    renderApp("/");
    await screen.findByRole("heading", { name: "Dashboard" });
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Start here" })).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "Nothing here yet" })).not.toBeInTheDocument();
  });

  it("does not send a read-only member to forms they cannot use", async () => {
    mockApi(handlers([], "viewer"));
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Nothing here yet" });
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
  });
});
