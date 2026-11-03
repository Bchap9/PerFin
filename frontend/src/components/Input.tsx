// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, type InputHTMLAttributes } from "react";

import { cx } from "./cx";
import { useFieldControl } from "./Field";

export type InputProps = InputHTMLAttributes<HTMLInputElement>;

/** A text-like input. Put it inside a `Field` for its label and error. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(props, ref) {
  const { className, type = "text", ...rest } = useFieldControl(props);
  return <input ref={ref} type={type} className={cx("input", className)} {...rest} />;
});
