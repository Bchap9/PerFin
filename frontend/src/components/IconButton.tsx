// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import type { ButtonSize, ButtonVariant } from "./Button";
import { cx } from "./cx";

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "aria-label"> {
  /** Accessible name; also shown as a tooltip. Required: the button has no text. */
  label: string;
  icon: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, variant = "ghost", size = "md", type = "button", className, title, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={title ?? label}
      className={cx("btn", "btn--icon", `btn--${variant}`, `btn--${size}`, className)}
      {...rest}
    >
      <span aria-hidden="true" className="btn__icon">
        {icon}
      </span>
    </button>
  );
});
