// SPDX-License-Identifier: AGPL-3.0-or-later
import { cx } from "./cx";

export interface SpinnerProps {
  /** Announced to assistive technology. */
  label?: string;
  size?: "sm" | "md" | "lg";
  /** Purely visual (e.g. inside a busy button that already says so). */
  decorative?: boolean;
  className?: string;
}

export function Spinner({ label = "Loading", size = "md", decorative = false, className }: SpinnerProps) {
  const ring = <span className={cx("spinner", `spinner--${size}`, className)} aria-hidden="true" />;
  if (decorative) {
    return ring;
  }
  return (
    <span role="status" className="spinner-wrap">
      {ring}
      <span className="visually-hidden">{label}</span>
    </span>
  );
}
