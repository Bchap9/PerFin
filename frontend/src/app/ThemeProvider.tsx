// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { IconButton } from "../components/IconButton";
import { MoonIcon, SunIcon, SystemIcon } from "../components/icons";
import {
  applyTheme,
  isThemePreference,
  nextTheme,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  systemPrefersDark,
  THEME_STORAGE_KEY,
  watchSystemTheme,
  type ResolvedTheme,
  type ThemePreference,
} from "./theme";

interface ThemeContextValue {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
  /** Move to the next theme (system -> light -> dark), from the latest state. */
  cycle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(readStoredTheme);
  const [prefersDark, setPrefersDark] = useState<boolean>(systemPrefersDark);

  useEffect(() => {
    applyTheme(preference);
    storeTheme(preference);
  }, [preference]);

  useEffect(() => watchSystemTheme(setPrefersDark), []);

  // Keep tabs in step: another tab changed the stored choice.
  useEffect(() => {
    const onStorage = (event: StorageEvent): void => {
      if (event.key === THEME_STORAGE_KEY || event.key === null) {
        setPreferenceState(isThemePreference(event.newValue) ? event.newValue : "system");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
  }, []);

  const cycle = useCallback(() => {
    setPreferenceState(nextTheme);
  }, []);

  const value = useMemo(
    () => ({ preference, resolved: resolveTheme(preference, prefersDark), setPreference, cycle }),
    [preference, prefersDark, setPreference, cycle],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) {
    throw new Error("useTheme must be used inside <ThemeProvider>");
  }
  return value;
}


/** Cycles system -> light -> dark. The name states the current and next theme. */
export function ThemeToggle() {
  const { preference, cycle } = useTheme();
  const next = nextTheme(preference);
  const icon = preference === "light" ? <SunIcon /> : preference === "dark" ? <MoonIcon /> : <SystemIcon />;
  return (
    <IconButton
      label={`Theme: ${preference}. Switch to ${next}.`}
      icon={icon}
      data-theme-preference={preference}
      onClick={cycle}
    />
  );
}
