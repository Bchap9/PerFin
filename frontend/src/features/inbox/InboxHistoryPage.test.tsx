// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import type { ReviewItem } from "../review/api";
import { lookupHandlers, NOW } from "../transactions/fixtures";

// Invented fixtures: no real people, banks or account numbers.

function item(id: string, kind: ReviewItem["kind"], state: ReviewItem["state"]): ReviewItem {
  return {
    id,
    kind,
    state,
    document_id: null,
    payload: {},
    resolution: null,
    resolved_at: state === "open" ? null : NOW,
    resolved_by: null,
    created_at: NOW,
    next_step: null,
    can_resolve: true,
  };
}

const RESOLVED = item("r-1", "unparsed", "resolved");
const DISMISSED = item("r-2", "possible_duplicate", "dismissed");
const OPEN = item("r-3", "unknown_account", "open");

function historyApi() {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "member" }),
    ...lookupHandlers(),
    "GET /api/v1/inbox/summary": () =>
      json({ documents: 0, loose: 0, subscriptions: 0, suggestions: 0, uncategorised: 0, cards: 0 }),
    "GET /api/v1/review": (call) => {
      const state = new URLSearchParams(call.search).get("state");
      const all = [RESOLVED, DISMISSED, OPEN];
      return json({ items: all.filter((one) => state === "all" || one.state === state), next_cursor: null });
    },
  });
}

describe("Inbox, History", () => {
  it("lists resolved items by default and switches to dismissed", async () => {
    const api = historyApi();
    renderApp("/inbox/history");
    const list = within(await screen.findByRole("list", { name: "Answered items" }));
    expect(await list.findByRole("heading", { name: "Could not be read" })).toBeInTheDocument();
    expect(list.getByText("Resolved")).toBeInTheDocument();
    expect(list.queryByRole("heading", { name: "Possible duplicate" })).not.toBeInTheDocument();
    expect(new URLSearchParams(api.callsTo("GET", "/api/v1/review")[0]?.search).get("state")).toBe("resolved");

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Show"), "dismissed");
    expect(await screen.findByRole("heading", { name: "Possible duplicate" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Could not be read" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(new URLSearchParams(api.callsTo("GET", "/api/v1/review").at(-1)?.search).get("state")).toBe("dismissed");
    });
  });

  it("all lists every answered item, never an open one", async () => {
    historyApi();
    renderApp("/inbox/history?show=all");
    const list = within(await screen.findByRole("list", { name: "Answered items" }));
    expect(await list.findByRole("heading", { name: "Could not be read" })).toBeInTheDocument();
    expect(list.getByRole("heading", { name: "Possible duplicate" })).toBeInTheDocument();
    expect(list.queryByRole("heading", { name: "Which account?" })).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    historyApi();
    const { container } = renderApp("/inbox/history");
    await screen.findByRole("heading", { name: "Could not be read" });
    await expectNoAxeViolations(container);
  });
});
