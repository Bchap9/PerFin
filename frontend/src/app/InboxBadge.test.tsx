// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";

import { json, mockApi, problem } from "../features/auth/testing";
import { expectNoAxeViolations } from "../test/axe";
import { InboxBadge } from "./InboxBadge";

const summary = (cards: number) => ({ cards, documents: cards, loose: 0, subscriptions: 0, suggestions: 0, uncategorised: 0 });

function renderBadge() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <a href="/inbox">
        <span>Inbox</span>
        <InboxBadge />
      </a>
    </QueryClientProvider>,
  );
}

describe("Inbox badge", () => {
  it('the name reads "3 things need you"', async () => {
    mockApi({ "GET /api/v1/inbox/summary": () => json(summary(3)) });
    const { container } = renderBadge();
    expect(await screen.findByRole("link", { name: "Inbox, 3 things need you" })).toBeInTheDocument();
    expect(container.querySelector(".badge")?.textContent).toBe("3");
    // Not a live region: a new count does not interrupt.
    expect(container.querySelector("[aria-live]")).toBeNull();
    await expectNoAxeViolations(container);
  });

  it("renders nothing at zero", async () => {
    const api = mockApi({ "GET /api/v1/inbox/summary": () => json(summary(0)) });
    const { container } = renderBadge();
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/inbox/summary")).toHaveLength(1);
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.querySelector(".badge")).toBeNull();
    expect(screen.getByRole("link", { name: "Inbox" })).toBeInTheDocument();
  });

  it("renders nothing on an error, not zero", async () => {
    const api = mockApi({ "GET /api/v1/inbox/summary": () => problem(500, "server-error") });
    const { container } = renderBadge();
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/inbox/summary")).toHaveLength(1);
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.querySelector(".badge")).toBeNull();
    expect(screen.getByRole("link", { name: "Inbox" })).toBeInTheDocument();
  });
});
