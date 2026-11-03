// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A modal dialog on the native `<dialog>` element: `showModal()` gives focus
 * trapping, `inert` background, Escape handling and top-layer stacking from
 * the browser. Controlled by `open`; Escape and the close button call
 * `onClose`. Focus returns to the element that had it before opening.
 */
import { useEffect, useId, useRef, type ReactNode } from "react";

import { cx } from "./cx";
import { IconButton } from "./IconButton";
import { CloseIcon } from "./icons";

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Action buttons, rendered at the bottom. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Label for the close button. */
  closeLabel?: string;
  className?: string;
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  closeLabel = "Close",
  className,
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) {
      return undefined;
    }
    if (!open) {
      if (dialog.open) {
        dialog.close();
      }
      return undefined;
    }
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        // Environments without showModal (old engines, jsdom): open non-modally.
        dialog.setAttribute("open", "");
      }
    }
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

  return (
    <dialog
      ref={ref}
      className={cx("dialog", `dialog--${size}`, className)}
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      onCancel={(event) => {
        // Escape: let the parent decide (it owns `open`).
        event.preventDefault();
        onCloseRef.current();
      }}
    >
      <div className="dialog__header">
        <h2 id={titleId} className="dialog__title">
          {title}
        </h2>
        <IconButton label={closeLabel} icon={<CloseIcon />} size="sm" onClick={() => { onCloseRef.current(); }} />
      </div>
      {description === undefined ? null : (
        <p id={descriptionId} className="dialog__description">
          {description}
        </p>
      )}
      <div className="dialog__body">{children}</div>
      {footer === undefined ? null : <div className="dialog__footer">{footer}</div>}
    </dialog>
  );
}
