// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Headline figures: a `StatGroup` is a description list, each `Stat` a term
 * (the label) and its value, with an optional change. The change says its
 * direction in words ("up 8%"), so colour is never the only signal.
 */
import type { ReactNode } from "react";

import { cx } from "./cx";

export type StatTone = "up" | "down" | "neutral";

export interface StatProps {
  label: string;
  value: ReactNode;
  change?: { text: string; tone: StatTone };
  size?: "md" | "display";
}

export function Stat({ label, value, change, size = "md" }: StatProps) {
  return (
    <div className={cx("stat", `stat--${size}`)}>
      <dt className="stat__label">{label}</dt>
      <dd className="stat__value num">{value}</dd>
      {change === undefined ? null : (
        <dd className={cx("stat__change", `stat__change--${change.tone}`)}>{change.text}</dd>
      )}
    </div>
  );
}

export interface StatGroupProps {
  children: ReactNode;
  /** An accessible name for the group, when the heading above does not give one. */
  label?: string;
  className?: string;
}

export function StatGroup({ children, label, className }: StatGroupProps) {
  return (
    <dl className={cx("stat-group", className)} aria-label={label}>
      {children}
    </dl>
  );
}
