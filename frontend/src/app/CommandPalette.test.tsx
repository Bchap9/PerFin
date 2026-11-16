// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi, sessionBody } from "../features/auth/testing";
import { queryRoots } from "../lib/queryKeys";
import { expectNoAxeViolations } from "../test/axe";
import { CommandPalette } from "./CommandPalette";

const ACCOUNT_ID = "a0000000-0000-4000-8000-0000000000aa";

/** A small router: the palette in a shell, a few pages that name themselves. */
function renderPalette(options: { accounts?: unknown[]; instanceAdmin?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(queryRoots.session, {
    status: "authenticated",
    csrfToken: "session-token",
    data: sessionBody({ instanceAdmin: options.instanceAdmin ?? false }),
  });
  if (options.accounts !== undefined) {
    queryClient.setQueryData([...queryRoots.accounts, "all"], options.accounts);
  }
  const root = createRootRoute({
    component: () => (
      <>
        <button type="button">Before</button>
        <label>
          Notes
          <input type="text" />
        </label>
        <Outlet />
        <CommandPalette />
      </>
    ),
  });
  const page = (path: string, name: string) =>
    createRoute({ getParentRoute: () => root, path, component: () => <p>{`Page ${name}`}</p> });
  const router = createRouter({
    routeTree: root.addChildren([
      page("/", "home"),
      page("/plan/budgets", "budgets"),
      page("/reports", "reports"),
      page("/accounts/$accountId", "account"),
    ]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, queryClient, ...view };
}

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard("{Control>}k{/Control}");
  return screen.findByRole("combobox", { name: "Go to" });
}

describe("CommandPalette", () => {
  it("Ctrl+K opens and Escape closes, focus returns", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderPalette();
    const before = await screen.findByRole("button", { name: "Before" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    before.focus();

    const input = await openPalette(user);
    expect(screen.getByRole("dialog", { name: "Go to a page" })).toBeInTheDocument();
    expect(input).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(before).toHaveFocus();

    // Cmd+K toggles too, open and closed.
    await user.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByRole("combobox", { name: "Go to" })).toHaveFocus();
    await user.keyboard("{Meta>}k{/Meta}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a bare k never opens it, in a text field or out of one", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderPalette();
    const notes = await screen.findByRole("textbox", { name: "Notes" });
    await user.click(notes);
    await user.keyboard("k");
    expect(notes).toHaveValue("k");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // With the modifier held it opens even from the text field.
    await openPalette(user);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("typing filters and Enter navigates", async () => {
    mockApi({});
    const user = userEvent.setup();
    const { router } = renderPalette();
    await screen.findByText("Page home");
    const input = await openPalette(user);

    await user.type(input, "budg");
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["Plan: Budgets"]);

    await user.keyboard("{Enter}");
    expect(await screen.findByText("Page budgets")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/plan/budgets");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Down moves to the next match and Enter carries its search", async () => {
    mockApi({});
    const user = userEvent.setup();
    const { router } = renderPalette();
    await screen.findByText("Page home");
    const input = await openPalette(user);
    await user.type(input, "reports: ");
    const options = screen.getAllByRole("option");
    expect(options.at(0)).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(options.at(1)).toHaveTextContent("Reports: Income");
    expect(options.at(1)).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Page reports")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ tab: "income" });
  });

  it("accounts already loaded are listed", async () => {
    const api = mockApi({});
    const user = userEvent.setup();
    const { router } = renderPalette({
      accounts: [{ id: ACCOUNT_ID, name: "Sample checking", nickname: null }],
    });
    await screen.findByText("Page home");
    const input = await openPalette(user);
    await user.type(input, "sample");
    expect(screen.getByRole("option", { name: "Account: Sample checking" })).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Page account")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/accounts/${ACCOUNT_ID}`);
    expect(api.callsTo("GET", "/api/v1/accounts")).toHaveLength(0);
  });

  it("says so when nothing matches, and fetches no accounts", async () => {
    const api = mockApi({});
    const user = userEvent.setup();
    renderPalette();
    const input = await openPalette(user);
    await user.type(input, "zzqx");
    expect(screen.getByRole("status")).toHaveTextContent("No page or account matches.");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(input).not.toHaveAttribute("aria-activedescendant");
    expect(api.callsTo("GET", "/api/v1/accounts")).toHaveLength(0);
  });

  it("the combobox pattern attributes", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderPalette();
    const input = await openPalette(user);
    const listbox = screen.getByRole("listbox");
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-controls", listbox.id);
    expect(input).toHaveAttribute("aria-autocomplete", "list");
    const first = within(listbox).getAllByRole("option").at(0);
    expect(first).toHaveTextContent("Home");
    expect(input).toHaveAttribute("aria-activedescendant", first?.id);
    await user.keyboard("{ArrowDown}");
    const second = within(listbox).getAllByRole("option").at(1);
    expect(input).toHaveAttribute("aria-activedescendant", second?.id);
    await user.keyboard("{ArrowUp}{ArrowUp}");
    // Up from the first wraps to the last.
    const last = within(listbox).getAllByRole("option").at(-1);
    expect(input).toHaveAttribute("aria-activedescendant", last?.id);
  });

  it("lists the Users page only for an instance administrator", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderPalette({ instanceAdmin: true });
    const input = await openPalette(user);
    await user.type(input, "users");
    expect(screen.getByRole("option", { name: "Settings: Users" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    mockApi({});
    const user = userEvent.setup();
    renderPalette({ accounts: [{ id: ACCOUNT_ID, name: "Sample checking", nickname: null }] });
    await openPalette(user);
    await expectNoAxeViolations(document.body);
  });
});
