// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The actions of one grid row behind a single menu button, so a row stays
 * one line tall however many actions it has.
 *
 * It follows the WAI-ARIA menu button pattern: the button opens the menu and
 * moves focus into it (Enter, Space, ArrowDown or a click focus the first
 * item, ArrowUp the last); ArrowUp/ArrowDown move with wrap-around, Home and
 * End jump, a letter jumps to the next item starting with it, Escape or
 * Shift+Tab closes and returns to the button, Tab or a click elsewhere
 * closes. The menu is `position: fixed`, placed beside the button when it
 * opens, so the table's scroll container never clips it; it follows the
 * button when the page scrolls or resizes and closes once the button is out
 * of view.
 */
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "../../components/Button";
import { cx } from "../../components/cx";

export interface RowAction {
  id: string;
  label: string;
  onSelect: () => void;
  /** A destructive action (delete) is set apart. */
  danger?: boolean;
}

/** Three dots: "more actions". Decorative; the button carries the name. */
function MoreIcon() {
  return (
    <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden="true" focusable="false">
      <circle cx="5" cy="12" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="19" cy="12" r="2" />
    </svg>
  );
}

const EDGE_GAP = 8;
const BUTTON_GAP = 4;

/**
 * Put the open menu under the button (above it when there is no room below),
 * inside the viewport. Answers `false` when the button itself is out of view.
 */
function place(menu: HTMLElement, button: HTMLElement): boolean {
  const anchor = button.getBoundingClientRect();
  if (anchor.bottom < 0 || anchor.top > window.innerHeight || anchor.right < 0 || anchor.left > window.innerWidth) {
    return false;
  }
  const size = menu.getBoundingClientRect();
  const below = anchor.bottom + BUTTON_GAP;
  const fitsBelow = below + size.height <= window.innerHeight - EDGE_GAP;
  const top = fitsBelow ? below : Math.max(EDGE_GAP, anchor.top - BUTTON_GAP - size.height);
  // End-aligned with the button, or start-aligned when there is no room on that side; always inside the viewport.
  const preferred = anchor.right - size.width >= EDGE_GAP ? anchor.right - size.width : anchor.left;
  const left = Math.max(EDGE_GAP, Math.min(preferred, window.innerWidth - EDGE_GAP - size.width));
  menu.style.top = `${String(Math.round(top))}px`;
  menu.style.left = `${String(Math.round(left))}px`;
  return true;
}

function menuItems(menu: HTMLElement | null): HTMLButtonElement[] {
  return Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
}

export function RowActions({ label, actions }: { label: string; actions: readonly RowAction[] }) {
  const menuId = useId();
  const [open, setOpen] = useState<"first" | "last" | null>(null);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (open === null || menuRef.current === null || buttonRef.current === null) {
      return;
    }
    place(menuRef.current, buttonRef.current);
    const all = menuItems(menuRef.current);
    (open === "last" ? all.at(-1) : all[0])?.focus({ preventScroll: true });
  }, [open]);

  useEffect(() => {
    if (open === null) {
      return undefined;
    }
    const close = (): void => {
      setOpen(null);
    };
    const onPointerDown = (event: PointerEvent): void => {
      if (event.target instanceof Node && wrapRef.current?.contains(event.target) !== true) {
        close();
      }
    };
    // The page moved under the menu: keep it at its button, or close it when the button has left the screen.
    const follow = (): void => {
      if (menuRef.current === null || buttonRef.current === null || !place(menuRef.current, buttonRef.current)) {
        close();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("scroll", follow, true);
    window.addEventListener("resize", follow);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
    };
  }, [open]);

  const closeToButton = (): void => {
    setOpen(null);
    buttonRef.current?.focus();
  };

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const all = menuItems(menuRef.current);
    const index = all.findIndex((item) => item === document.activeElement);
    let next: HTMLButtonElement | undefined;
    if (event.key === "ArrowDown") {
      next = all[(index + 1) % all.length];
    } else if (event.key === "ArrowUp") {
      next = all[(index - 1 + all.length) % all.length];
    } else if (event.key === "Home") {
      next = all[0];
    } else if (event.key === "End") {
      next = all.at(-1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeToButton();
      return;
    } else if (event.key === "Tab") {
      // Forwards, focus leaves for the next control and the blur closes the menu; backwards is the button.
      if (event.shiftKey) {
        event.preventDefault();
        closeToButton();
      }
      return;
    } else if (event.key.length === 1 && event.key !== " " && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const letter = event.key.toLowerCase();
      const after = [...all.slice(index + 1), ...all.slice(0, index + 1)];
      next = after.find((item) => item.textContent.trim().toLowerCase().startsWith(letter));
      if (next === undefined) {
        return;
      }
    } else {
      return;
    }
    event.preventDefault();
    next?.focus();
  };

  return (
    <span className="row-menu" ref={wrapRef}>
      <Button
        ref={buttonRef}
        size="sm"
        variant="ghost"
        className="row-menu__button"
        icon={<MoreIcon />}
        aria-label={`Actions for ${label}`}
        title="Actions"
        aria-haspopup="menu"
        aria-expanded={open !== null}
        aria-controls={open === null ? undefined : menuId}
        onClick={() => {
          setOpen((current) => (current === null ? "first" : null));
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(event.key === "ArrowUp" ? "last" : "first");
          }
        }}
      >
        Actions
      </Button>
      {open === null ? null : (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={`Actions for ${label}`}
          className="row-menu__list"
          onKeyDown={onMenuKeyDown}
          onBlur={(event) => {
            // Focus left the menu for somewhere outside it (not for its own button, which toggles).
            if (event.relatedTarget instanceof Node && wrapRef.current?.contains(event.relatedTarget) !== true) {
              setOpen(null);
            }
          }}
        >
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className={cx("row-menu__item", action.danger === true && "row-menu__item--danger")}
              onClick={() => {
                // Back to the button first, so a dialog the action opens returns focus there when it closes.
                closeToButton();
                action.onSelect();
              }}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
