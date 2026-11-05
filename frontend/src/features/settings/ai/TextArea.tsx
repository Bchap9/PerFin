// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, type TextareaHTMLAttributes } from "react";

import { cx } from "../../../components/cx";
import { useFieldControl } from "../../../components/Field";

/** A multi-line text control. Put it inside a `Field` for its label and error. */
export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function TextArea(
  props,
  ref,
) {
  const { className, ...rest } = useFieldControl(props);
  return <textarea ref={ref} className={cx("input", "ai-textarea", className)} {...rest} />;
});

/** One term per line: trimmed, without blanks or repeats. */
export function parseTerms(text: string): string[] {
  const seen = new Set<string>();
  for (const line of text.split("\n")) {
    const term = line.trim();
    if (term !== "") {
      seen.add(term);
    }
  }
  return [...seen];
}
