// SPDX-License-Identifier: AGPL-3.0-or-later
import type { HTMLAttributes } from "react";

import { cx } from "./cx";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

/** A short status label. Colour is never the only signal: the text says it. */
export function Badge({ tone = "neutral", className, ...rest }: BadgeProps) {
  return <span className={cx("badge", `badge--${tone}`, className)} {...rest} />;
}
