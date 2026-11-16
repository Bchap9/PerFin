// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A data table with a sticky header. At narrow widths (CSS `.table`) each row
 * becomes a card of label and value pairs (from `data-label`), so nothing needs
 * sideways scrolling; the markup stays a real table. The scroll container is a focusable,
 * labelled region so keyboard users can scroll it; the caption names both
 * the table and the region (it can be visually hidden).
 *
 * `rowLink` makes a row open something (a drawer, a page) without making the
 * row a click-only target: the row's first cell then starts with a real
 * button (`table__row-link`) carrying the label, and a click elsewhere on the
 * row, or Enter on the row itself, only forwards to that button. Controls
 * inside the row keep their own clicks.
 */
import { useId, type Key, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";

import { cx } from "./cx";

export interface TableColumn<Row> {
  id: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  /** `end` for numbers and money. */
  align?: "start" | "end" | "center";
  /** Render this column's cells as row headers (`<th scope="row">`). */
  rowHeader?: boolean;
  /**
   * Text shown beside the value when the table collapses to cards on narrow
   * screens. Defaults to `header` when that is a string; a column with no
   * label (an actions column) shows only its value.
   */
  label?: string;
  className?: string;
}

function cellLabel<Row>(column: TableColumn<Row>): string | undefined {
  return column.label ?? (typeof column.header === "string" ? column.header : undefined);
}

export interface TableProps<Row> {
  caption: ReactNode;
  captionHidden?: boolean;
  columns: readonly TableColumn<Row>[];
  rows: readonly Row[];
  rowKey: (row: Row) => Key;
  /** Shown in place of the body when there are no rows. */
  empty?: ReactNode;
  footer?: ReactNode;
  /** Limit the height so the header sticks while the body scrolls. */
  maxHeight?: "sm" | "md" | "lg" | "none";
  density?: "comfortable" | "compact";
  className?: string;
  /** What the row opens, or null for a row that opens nothing. */
  rowLink?: (row: Row) => { label: string; onOpen: () => void } | null;
}

const INTERACTIVE = "a, button, input, select, textarea, label, summary, [role='button'], [role='menuitem'], [role='checkbox']";

/** The row's link button, when the event did not start on a control of its own. */
function forwardTarget(event: MouseEvent<HTMLTableRowElement> | KeyboardEvent<HTMLTableRowElement>): HTMLButtonElement | null {
  const target = event.target;
  if (target instanceof Element && target !== event.currentTarget && target.closest(INTERACTIVE) !== null) {
    return null;
  }
  return event.currentTarget.querySelector<HTMLButtonElement>("button.table__row-link");
}

export function Table<Row>({
  caption,
  captionHidden = false,
  columns,
  rows,
  rowKey,
  empty,
  footer,
  maxHeight = "lg",
  density = "comfortable",
  className,
  rowLink,
}: TableProps<Row>) {
  const captionId = useId();
  return (
    <div
      className={cx("table-wrap", `table-wrap--${maxHeight}`, className)}
      role="region"
      aria-labelledby={captionId}
      // A scrollable region must be reachable by keyboard (WCAG 2.1.1).
      tabIndex={0}
    >
      <table className={cx("table", `table--${density}`)}>
        <caption id={captionId} className={cx("table__caption", captionHidden && "visually-hidden")}>
          {caption}
        </caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.id} scope="col" className={cx(`align-${column.align ?? "start"}`, column.className)}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && empty !== undefined ? (
            <tr>
              <td colSpan={columns.length} className="table__empty">
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((row) => {
              const link = rowLink?.(row) ?? null;
              const content = (column: TableColumn<Row>, index: number): ReactNode =>
                link !== null && index === 0 ? (
                  <>
                    <button type="button" className="table__row-link" onClick={link.onOpen}>
                      {link.label}
                    </button>
                    {column.cell(row)}
                  </>
                ) : (
                  column.cell(row)
                );
              return (
                <tr
                  key={rowKey(row)}
                  className={link === null ? undefined : "table__row--link"}
                  onClick={
                    link === null
                      ? undefined
                      : (event) => {
                          forwardTarget(event)?.click();
                        }
                  }
                  onKeyDown={
                    link === null
                      ? undefined
                      : (event) => {
                          if (event.key === "Enter" && event.target === event.currentTarget) {
                            event.preventDefault();
                            forwardTarget(event)?.click();
                          }
                        }
                  }
                >
                  {columns.map((column, index) =>
                    column.rowHeader === true ? (
                      <th key={column.id} scope="row" data-label={cellLabel(column)} className={cx(`align-${column.align ?? "start"}`, column.className)}>
                        {content(column, index)}
                      </th>
                    ) : (
                      <td key={column.id} data-label={cellLabel(column)} className={cx(`align-${column.align ?? "start"}`, column.className)}>
                        {content(column, index)}
                      </td>
                    ),
                  )}
                </tr>
              );
            })
          )}
        </tbody>
        {footer === undefined ? null : <tfoot>{footer}</tfoot>}
      </table>
    </div>
  );
}
