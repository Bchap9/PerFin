// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from "react";

import { cx } from "./cx";

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
}

/** A checkbox with its own label (and optional hint and error). */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, hint, error, id, className, ...rest },
  ref,
) {
  const generated = useId();
  const controlId = id ?? `checkbox-${generated}`;
  const hintId = hint === undefined || hint === null ? undefined : `${controlId}-hint`;
  const hasError = error !== undefined && error !== null && error !== false && error !== "";
  const errorId = hasError ? `${controlId}-error` : undefined;
  const describedBy = [hintId, errorId, rest["aria-describedby"]].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("checkbox", hasError && "checkbox--invalid", className)}>
      <input
        ref={ref}
        type="checkbox"
        id={controlId}
        className="checkbox__control"
        {...rest}
        aria-describedby={describedBy}
        aria-invalid={hasError || undefined}
      />
      <label className="checkbox__label" htmlFor={controlId}>
        {label}
      </label>
      {hintId === undefined ? null : (
        <p id={hintId} className="field__hint checkbox__hint">
          {hint}
        </p>
      )}
      {errorId === undefined ? null : (
        <p id={errorId} className="field__error checkbox__hint">
          {error}
        </p>
      )}
    </div>
  );
});
