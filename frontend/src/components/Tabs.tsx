// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * WAI-ARIA tabs with automatic activation: arrow keys move between tabs (and
 * select them), Home/End jump to the ends, only the selected tab is in the
 * tab order. Controlled (`selectedId` + `onSelect`) or uncontrolled.
 */
import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { cx } from "./cx";

export interface TabItem {
  id: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
}

export interface TabsProps {
  /** Accessible name of the tab list. */
  label: string;
  tabs: readonly TabItem[];
  selectedId?: string;
  defaultSelectedId?: string;
  onSelect?: (id: string) => void;
  className?: string;
}

export function Tabs({ label, tabs, selectedId, defaultSelectedId, onSelect, className }: TabsProps) {
  const baseId = useId();
  const firstEnabled = tabs.find((tab) => tab.disabled !== true)?.id;
  const [internal, setInternal] = useState(defaultSelectedId ?? firstEnabled);
  const current = selectedId ?? internal;
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());

  const select = (id: string): void => {
    if (selectedId === undefined) {
      setInternal(id);
    }
    onSelect?.(id);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const enabled = tabs.filter((tab) => tab.disabled !== true);
    const index = enabled.findIndex((tab) => tab.id === current);
    let target: TabItem | undefined;
    switch (event.key) {
      case "ArrowRight":
        target = enabled[(index + 1) % enabled.length];
        break;
      case "ArrowLeft":
        target = enabled[(index - 1 + enabled.length) % enabled.length];
        break;
      case "Home":
        target = enabled[0];
        break;
      case "End":
        target = enabled[enabled.length - 1];
        break;
      default:
        return;
    }
    if (target !== undefined) {
      event.preventDefault();
      select(target.id);
      tabRefs.current.get(target.id)?.focus();
    }
  };

  const tabId = (id: string): string => `${baseId}-tab-${id}`;
  const panelId = (id: string): string => `${baseId}-panel-${id}`;

  return (
    <div className={cx("tabs", className)}>
      <div role="tablist" aria-label={label} className="tabs__list" onKeyDown={onKeyDown}>
        {tabs.map((tab) => {
          const selected = tab.id === current;
          return (
            <button
              key={tab.id}
              ref={(node) => {
                if (node === null) {
                  tabRefs.current.delete(tab.id);
                } else {
                  tabRefs.current.set(tab.id, node);
                }
              }}
              type="button"
              role="tab"
              id={tabId(tab.id)}
              aria-selected={selected}
              aria-controls={panelId(tab.id)}
              tabIndex={selected ? 0 : -1}
              disabled={tab.disabled}
              className={cx("tabs__tab", selected && "tabs__tab--selected")}
              onClick={() => {
                select(tab.id);
              }}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={panelId(tab.id)}
          aria-labelledby={tabId(tab.id)}
          hidden={tab.id !== current}
          tabIndex={0}
          className="tabs__panel"
        >
          {tab.id === current ? tab.content : null}
        </div>
      ))}
    </div>
  );
}
