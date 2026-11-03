// SPDX-License-Identifier: AGPL-3.0-or-later
import "./app/styles/tokens.css";
import "./app/styles/base.css";
import "./app/styles/layout.css";
import "./components/components.css";

import { QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ErrorBoundary } from "./app/ErrorBoundary";
import { createAppRouter } from "./app/router";
import { applyTheme, readStoredTheme } from "./app/theme";
import { isApiError } from "./lib/api/client";

// Apply the stored theme before the first paint (no inline script: CSP).
applyTheme(readStoredTheme());

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Do not retry what will not change: auth, permission, not-found, validation.
      retry: (failureCount, error) =>
        failureCount < 1 && !(isApiError(error) && error.status >= 400 && error.status < 500),
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});
const router = createAppRouter({ queryClient });

const container = document.getElementById("root");
if (container === null) {
  throw new Error("missing #root element");
}

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <RouterProvider router={router} />
    </ErrorBoundary>
  </StrictMode>,
);
