// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A labelled form field. `Field` renders the label, hint and error and
 * passes `id`, `aria-describedby`, `aria-invalid` and `required` to the
 * control inside it (Input, Select) through context, so a field is always
 * labelled and its error is announced with it.
 */
import { createContext, useContext, useId, type ReactNode } from "react";

import { cx } from "./cx";

interface FieldContextValue {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
  required: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

export interface FieldProps {
  label: ReactNode;
  children: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** Use a fixed id for the control instead of a generated one. */
  id?: string;
  className?: string;
}

export function Field({ label, children, hint, error, required = false, id, className }: FieldProps) {
  const generated = useId();
  const controlId = id ?? `field-${generated}`;
  const hintId = hint === undefined || hint === null ? undefined : `${controlId}-hint`;
  const hasError = error !== undefined && error !== null && error !== false && error !== "";
  const errorId = hasError ? `${controlId}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("field", hasError && "field--invalid", className)}>
      <label className="field__label" htmlFor={controlId}>
        {label}
        {required ? (
          <span className="field__required" aria-hidden="true">
            {" *"}
          </span>
        ) : null}
      </label>
      <FieldContext.Provider value={{ id: controlId, describedBy, invalid: hasError, required }}>
        {children}
      </FieldContext.Provider>
      {hintId === undefined ? null : (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      )}
      {errorId === undefined ? null : (
        <p id={errorId} className="field__error">
          {error}
        </p>
      )}
    </div>
  );
}

interface ControlAria {
  id?: string | undefined;
  "aria-describedby"?: string | undefined;
  "aria-invalid"?: boolean | "true" | "false" | "grammar" | "spelling" | undefined;
  required?: boolean | undefined;
}

/** Merge the surrounding Field's wiring into a control's props (explicit props win). */
export function useFieldControl<P extends ControlAria>(props: P): P {
  const field = useContext(FieldContext);
  if (field === null) {
    return props;
  }
  const describedBy = [field.describedBy, props["aria-describedby"]].filter(Boolean).join(" ") || undefined;
  return {
    ...props,
    id: props.id ?? field.id,
    "aria-describedby": describedBy,
    "aria-invalid": props["aria-invalid"] ?? (field.invalid ? true : undefined),
    required: props.required ?? (field.required ? true : undefined),
  };
}
