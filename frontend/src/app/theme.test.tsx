// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  applyTheme,
  nextTheme,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  THEME_STORAGE_KEY,
} from "./theme";
import { ThemeProvider, ThemeToggle, useTheme } from "./ThemeProvider";

function mockMatchMedia(dark: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => query));
  return {
    change(matches: boolean) {
      query.matches = matches;
      for (const listener of listeners) {
        listener({ matches } as MediaQueryListEvent);
      }
    },
  };
}

function Resolved() {
  return <output aria-label="resolved theme">{useTheme().resolved}</output>;
}

describe("theme helpers", () => {
  it("cycles system -> light -> dark -> system", () => {
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("system");
  });

  it("resolves system from the OS preference", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
  });

  it("persists choices in localStorage, and system removes the key", () => {
    storeTheme("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(readStoredTheme()).toBe("dark");
    storeTheme("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    window.localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(readStoredTheme()).toBe("system");
  });

  it("survives storage that throws", () => {
    const failing = {
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
      removeItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    };
    vi.spyOn(window, "localStorage", "get").mockReturnValue(failing as unknown as Storage);
    expect(readStoredTheme()).toBe("system");
    expect(() => {
      storeTheme("dark");
    }).not.toThrow();
  });

  it("sets and clears data-theme on <html>", () => {
    applyTheme("dark");
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    applyTheme("system");
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
  });
});

describe("ThemeToggle", () => {
  it("cycles the theme, updates <html> and persists the choice", async () => {
    mockMatchMedia(false);
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <ThemeToggle />
        <Resolved />
      </ThemeProvider>,
    );
    const toggle = screen.getByRole("button", { name: "Theme: system. Switch to light." });
    expect(screen.getByLabelText("resolved theme")).toHaveTextContent("light");

    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Theme: light. Switch to dark." })).toBeInTheDocument();
    expect(document.documentElement.dataset["theme"]).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");

    await user.click(toggle);
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    expect(screen.getByLabelText("resolved theme")).toHaveTextContent("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    await user.click(toggle);
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it("uses the latest state when clicked twice before re-rendering", () => {
    mockMatchMedia(false);
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    const toggle = screen.getByRole("button", { name: /^Theme: system\./ });
    act(() => {
      toggle.click();
      toggle.click();
    });
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("starts from the stored choice and follows the OS while on system", () => {
    const media = mockMatchMedia(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    const { unmount } = render(
      <ThemeProvider>
        <ThemeToggle />
        <Resolved />
      </ThemeProvider>,
    );
    expect(screen.getByRole("button", { name: /^Theme: light\./ })).toBeInTheDocument();
    expect(document.documentElement.dataset["theme"]).toBe("light");
    unmount();

    window.localStorage.clear();
    render(
      <ThemeProvider>
        <Resolved />
      </ThemeProvider>,
    );
    expect(screen.getByLabelText("resolved theme")).toHaveTextContent("dark");
    act(() => {
      media.change(false);
    });
    expect(screen.getByLabelText("resolved theme")).toHaveTextContent("light");
  });
});
