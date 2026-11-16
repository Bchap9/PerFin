// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, renderApp } from "../auth/testing";

const PREFERENCES = { display_name: "Alice", locale: "en-US", theme: "system", timezone: "UTC" };

function profileApi(instanceAdmin: boolean) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ instanceAdmin }),
    "GET /api/v1/households": () => json([]),
    "GET /api/v1/settings/user": () => json(PREFERENCES),
  });
}

async function sideList(): Promise<HTMLElement> {
  return screen.findByRole("navigation", { name: "Settings" });
}

describe("the settings side list", () => {
  it("lists the six groups in order", async () => {
    profileApi(true);
    renderApp("/settings/profile");
    const nav = await sideList();
    const groups = within(nav)
      .getAllByRole("heading")
      .map((heading) => heading.textContent);
    expect(groups).toEqual(["You", "Household", "Categorisation", "Import", "Data and integrations", "Instance"]);
    expect(within(nav).getByRole("link", { name: "Name and currency" })).toHaveAttribute("href", "/settings/general");
    expect(within(nav).getByRole("link", { name: "Members" })).toHaveAttribute("href", "/settings/household");
    expect(within(nav).getByRole("link", { name: "What PerFin learned" })).toHaveAttribute("href", "/settings/learned");
  });

  it("marks the current page", async () => {
    profileApi(false);
    renderApp("/settings/profile");
    const nav = await sideList();
    expect(within(nav).getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Security" })).not.toHaveAttribute("aria-current");
  });

  it("Users only for instance administrators", async () => {
    profileApi(false);
    renderApp("/settings/profile");
    const nav = await sideList();
    expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("heading", { name: "Instance" })).not.toBeInTheDocument();
  });

  it("is the index itself on /settings, so it is not repeated there", async () => {
    profileApi(false);
    renderApp("/settings");
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    profileApi(true);
    const { container } = renderApp("/settings/profile");
    await sideList();
    await screen.findByLabelText(/^Display name/);
    await expectNoAxeViolations(container);
  });
});
