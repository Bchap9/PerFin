// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Light/dark theme.
 *
 * Colours are CSS custom properties (src/app/styles/tokens.css). With no
 * explicit choice the `prefers-color-scheme` media query decides; a choice
 * sets `data-theme="light|dark"` on `<html>`, which overrides it. The choice
 * is a per-browser convenience kept in localStorage; storage can be missing
 * or throw (private windows, blocked site data), so every access is guarded
 * and the app falls back to the system preference.
 */

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "perfin.theme";
export const THEME_ORDER: readonly ThemePreference[] = ["system", "light", "dark"];
const DARK_QUERY = "(prefers-color-scheme: dark)";

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function readStoredTheme(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(value) ? value : "system";
  } catch {
    return "system";
  }
}

export function storeTheme(preference: ThemePreference): void {
  try {
    if (preference === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    // Storage unavailable: the choice lasts for this page view only.
  }
}

export function systemPrefersDark(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia(DARK_QUERY).matches;
  } catch {
    return false;
  }
}

export function resolveTheme(preference: ThemePreference, prefersDark: boolean = systemPrefersDark()): ResolvedTheme {
  if (preference === "system") {
    return prefersDark ? "dark" : "light";
  }
  return preference;
}

/** Reflect the preference on `<html>`; "system" removes the override. */
export function applyTheme(preference: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (preference === "system") {
    delete root.dataset["theme"];
  } else {
    root.dataset["theme"] = preference;
  }
}

export function nextTheme(preference: ThemePreference): ThemePreference {
  const index = THEME_ORDER.indexOf(preference);
  return THEME_ORDER[(index + 1) % THEME_ORDER.length] ?? "system";
}

/** Subscribe to OS theme changes; returns an unsubscribe function. */
export function watchSystemTheme(onChange: (prefersDark: boolean) => void): () => void {
  if (typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  try {
    const query = window.matchMedia(DARK_QUERY);
    const listener = (event: MediaQueryListEvent): void => {
      onChange(event.matches);
    };
    query.addEventListener("change", listener);
    return () => {
      query.removeEventListener("change", listener);
    };
  } catch {
    return () => undefined;
  }
}
