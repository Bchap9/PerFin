// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A small rounded label: a category (with a colour dot from
 * `lib/categoryColour.ts`), a state in one of five tones, or a toggle filter.
 * With `onClick` it is a button, and `pressed` makes it a toggle
 * (`aria-pressed`); otherwise it is plain text. At least 24px tall.
 */
import type { ReactNode } from "react";

import { cx } from "./cx";

export type ChipTone = "neutral" | "ok" | "warn" | "bad" | "info";

export interface ChipProps {
  tone?: ChipTone;
  /** `chip--s1` … `chip--s6` or `chip--none`: draws a dot in that colour. */
  colourClass?: string;
  pressed?: boolean;
  onClick?: () => void;
  children: ReactNode;
  className?: string;
}

export function Chip({ tone = "neutral", colourClass, pressed, onClick, children, className }: ChipProps) {
  const classes = cx("chip", `chip--${tone}`, colourClass, onClick !== undefined && "chip--button", className);
  const content = (
    <>
      {colourClass === undefined ? null : <span className="chip__dot" aria-hidden="true" />}
      <span className="chip__label">{children}</span>
    </>
  );
  if (onClick === undefined) {
    return <span className={classes}>{content}</span>;
  }
  return (
    <button type="button" className={classes} aria-pressed={pressed} onClick={onClick}>
      {content}
    </button>
  );
}
