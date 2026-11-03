// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, type SelectHTMLAttributes } from "react";

import { cx } from "./cx";
import { useFieldControl } from "./Field";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  /** Options to render; `children` may be used instead for groups. */
  options?: readonly SelectOption[];
  /** A first, empty option (e.g. "Choose an account"). */
  placeholder?: string;
}

/** A native select. Put it inside a `Field` for its label and error. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(props, ref) {
  const { className, options, placeholder, children, ...rest } = useFieldControl(props);
  return (
    <span className="select">
      <select ref={ref} className={cx("select__control", className)} {...rest}>
        {placeholder === undefined ? null : <option value="">{placeholder}</option>}
        {options?.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
        {children}
      </select>
    </span>
  );
});
