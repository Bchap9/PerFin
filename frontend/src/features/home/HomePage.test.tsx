// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";

import { createAppRouter } from "../../app/router";

describe("home route", () => {
  it("renders the placeholder home page at /", async () => {
    const router = createAppRouter(
      { queryClient: new QueryClient() },
      createMemoryHistory({ initialEntries: ["/"] }),
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByRole("heading", { name: "PerFin" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing to see here yet/)).toBeInTheDocument();
  });
});
