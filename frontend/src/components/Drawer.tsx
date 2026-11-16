// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A right-hand panel for the details of one thing (a transaction, a
 * document), on the native `<dialog>` element like `Dialog`: `showModal()`
 * makes the rest of the page inert and puts the panel in the top layer.
 *
 * Escape and the close button call `onClose`; the parent owns `open`. Focus
 * moves into the panel when it opens (to `initialFocus`, else the first
 * control) and returns to the element that had it when it closes. Tab and
 * Shift+Tab wrap inside the panel, so focus stays in it even where
 * `showModal` is missing. Under 820px the CSS makes it a full-height sheet;
 * it slides in only when the person has not asked for reduced motion.
 */
import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode, type RefObject } from "react";

import { IconButton } from "./IconButton";
import { CloseIcon } from "./icons";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: ReactNode;
  /** Actions at the bottom of the panel. */
  footer?: ReactNode;
  /** The control that gets focus when the panel opens (default: the first one). */
  initialFocus?: RefObject<HTMLElement>;
}

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((element) => !element.closest("[hidden]"));
}

export function Drawer({ open, onClose, title, description, children, footer, initialFocus }: DrawerProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const onCloseRef = useRef(onClose);
  const initialFocusRef = useRef(initialFocus);

  useEffect(() => {
    onCloseRef.current = onClose;
    initialFocusRef.current = initialFocus;
  }, [onClose, initialFocus]);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null || !open) {
      return undefined;
    }
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        // Environments without showModal (old engines, jsdom): open non-modally; the Tab wrap below still holds focus.
        dialog.setAttribute("open", "");
      }
    }
    const target = initialFocusRef.current?.current ?? focusables(dialog)[0];
    target?.focus();
    return () => {
      if (dialog.open) {
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      }
      previous?.focus();
    };
  }, [open]);

  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>): void => {
    // A dialog opened from inside the drawer handles its own keys (its Escape closes only it).
    if (event.target instanceof Element && event.target.closest("dialog") !== ref.current) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onCloseRef.current();
      return;
    }
    if (event.key !== "Tab" || ref.current === null) {
      return;
    }
    const all = focusables(ref.current);
    const first = all[0];
    const last = all.at(-1);
    if (first === undefined || last === undefined) {
      return;
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  if (!open) {
    return null;
  }

  return (
    <dialog
      ref={ref}
      className="drawer"
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      onKeyDown={onKeyDown}
      onCancel={(event) => {
        // Escape in a modal dialog: the parent decides (it owns `open`).
        event.preventDefault();
        onCloseRef.current();
      }}
    >
      <div className="drawer__header">
        <div className="drawer__heading">
          <h2 id={titleId} className="drawer__title">
            {title}
          </h2>
          {description === undefined ? null : (
            <p id={descriptionId} className="drawer__description">
              {description}
            </p>
          )}
        </div>
        <IconButton
          label="Close"
          icon={<CloseIcon />}
          size="sm"
          onClick={() => {
            onCloseRef.current();
          }}
        />
      </div>
      <div className="drawer__body">{children}</div>
      {footer === undefined ? null : <div className="drawer__footer">{footer}</div>}
    </dialog>
  );
}
