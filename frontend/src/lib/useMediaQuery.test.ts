// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, renderHook } from "@testing-library/react";

import { ICON_RAIL_MAX, PHONE_MAX, TABLE_CARDS_MAX, useMediaQuery } from "./useMediaQuery";

type Listener = (event: { matches: boolean }) => void;

/** A controllable `matchMedia`: one shared state per query string. */
function stubMatchMedia(initial: Record<string, boolean>) {
  const state = new Map(Object.entries(initial));
  const listeners = new Map<string, Set<Listener>>();
  const matchMedia = vi.fn((query: string) => ({
    get matches() {
      return state.get(query) ?? false;
    },
    media: query,
    addEventListener: (_type: "change", listener: Listener) => {
      const set = listeners.get(query) ?? new Set<Listener>();
      set.add(listener);
      listeners.set(query, set);
    },
    removeEventListener: (_type: "change", listener: Listener) => {
      listeners.get(query)?.delete(listener);
    },
  }));
  vi.stubGlobal("matchMedia", matchMedia);
  return {
    set(query: string, matches: boolean) {
      state.set(query, matches);
      for (const listener of listeners.get(query) ?? []) listener({ matches });
    },
    listenerCount: (query: string) => listeners.get(query)?.size ?? 0,
  };
}

describe("useMediaQuery", () => {
  it("names the three breakpoints of the layout", () => {
    expect(PHONE_MAX).toBe(819);
    expect(TABLE_CARDS_MAX).toBe(719);
    expect(ICON_RAIL_MAX).toBe(1023);
  });

  it("reads the query and follows its changes, then stops listening", () => {
    const query = `(max-width: ${String(PHONE_MAX)}px)`;
    const media = stubMatchMedia({ [query]: true });
    const { result, unmount } = renderHook(() => useMediaQuery(query));
    expect(result.current).toBe(true);
    act(() => {
      media.set(query, false);
    });
    expect(result.current).toBe(false);
    expect(media.listenerCount(query)).toBe(1);
    unmount();
    expect(media.listenerCount(query)).toBe(0);
  });

  it("answers false where matchMedia does not exist", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result } = renderHook(() => useMediaQuery("(max-width: 819px)"));
    expect(result.current).toBe(false);
  });
});
