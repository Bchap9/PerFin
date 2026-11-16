// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Parts of a whole as one SVG bar (`role="img"`, named by `label`), followed
 * by a legend list with every part and its amount, so the figures never live
 * only in the picture. Widths are SVG attributes (CSP `style-src 'self'`);
 * colours come from `colourClass` (`chip--s1` … `chip--s6`, from
 * `lib/categoryColour.ts`), which sets `--chip-colour` for the part and its swatch.
 */
import { cx } from "./cx";

export interface StackBarPart {
  label: string;
  value: number;
  colourClass: string;
}

export interface StackBarProps {
  label: string;
  parts: readonly StackBarPart[];
  /** Text of an amount in the legend (default: the number as is). */
  format?: (value: number) => string;
  className?: string;
}

interface Segment extends StackBarPart {
  x: number;
  width: number;
}

const round = (value: number): number => Math.round(value * 10) / 10;

function segments(parts: readonly StackBarPart[]): Segment[] {
  const positive = parts.filter((part) => Number.isFinite(part.value) && part.value > 0);
  const total = positive.reduce((sum, part) => sum + part.value, 0);
  if (total <= 0) {
    return [];
  }
  let x = 0;
  return positive.map((part) => {
    const width = round((part.value / total) * 100);
    const segment = { ...part, x: round(x), width };
    x += width;
    return segment;
  });
}

export function StackBar({ label, parts, format = String, className }: StackBarProps) {
  return (
    <div className={cx("stack", className)}>
      <svg className="stack__bar" viewBox="0 0 100 14" preserveAspectRatio="none" role="img" aria-label={label} focusable="false">
        <rect className="stack__track" x="0" y="0" width="100" height="14" />
        {segments(parts).map((part) => (
          <rect key={part.label} className={cx("stack__part", part.colourClass)} x={part.x} y="0" width={part.width} height="14" />
        ))}
      </svg>
      <ul className="stack__legend" aria-label={label}>
        {parts.map((part) => (
          <li key={part.label} className="stack__item">
            <span className={cx("stack__swatch", part.colourClass)} aria-hidden="true" />
            <span className="stack__label">{part.label}</span>
            <span className="stack__value num">{format(part.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
