// SPDX-License-Identifier: AGPL-3.0-or-later
import "../networth/wealth.css";

import { MoneyText } from "../../components/MoneyText";
import type { Money } from "../../lib/money";
import type { Allocation } from "./api";
import { gainDisplay, formatFraction, weightWidth } from "./format";

/** A gain or loss with its percent of cost; the sign and the words carry the meaning, colour only helps. */
export function GainText({ gain, cost }: { gain: Money; cost: Money }) {
  const view = gainDisplay(gain.amount, cost.amount);
  return (
    <span className={`gain--${view.tone}`}>
      <MoneyText amount={gain.amount} currency={gain.currency} tone="none" signDisplay="exceptZero" /> <span>({view.percentLabel})</span>
    </span>
  );
}

/** One allocation breakdown as labelled bars. */
export function AllocationBars({ title, slices, labels }: { title: string; slices: readonly Allocation[]; labels?: (key: string) => string }) {
  if (slices.length === 0) {
    return <p className="muted">Nothing to show.</p>;
  }
  return (
    <ul className="bar-list" aria-label={title}>
      {slices.map((slice) => {
        const name = labels === undefined ? slice.key : labels(slice.key);
        return (
          <li key={slice.key} className="bar-row">
            <span>{name}</span>
            <span className="bar" role="img" aria-label={`${name}: ${formatFraction(slice.weight, 1)} of the portfolio`}>
              <span className="bar__fill" style={{ width: weightWidth(slice.weight) }} />
            </span>
            <span>
              <MoneyText amount={slice.value.amount} currency={slice.value.currency} tone="none" /> ({formatFraction(slice.weight, 1)})
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function titleCase(key: string): string {
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
