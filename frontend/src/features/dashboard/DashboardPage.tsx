// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The home page: a set of widgets the person chooses from a catalogue
 * (net worth, cash flow, spending, budgets, goals, recent transactions,
 * review queue, upcoming bills, holdings, loans, tax documents, accounts),
 * orders and sizes. The layout is saved to the server per person
 * (`useDashboardLayout`). Rearranging uses buttons and a select, not
 * dragging, so it works with a keyboard, a screen reader and a thumb.
 */
import "./dashboard.css";

import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { cx } from "../../components/cx";
import { PlanningPage } from "../planning/PlanningPage";
import {
  addWidget,
  availableWidgets,
  DEFAULT_LAYOUT,
  moveWidget,
  removeWidget,
  resizeWidget,
  SIZE_OPTIONS,
  widgetInfo,
  type DashboardLayout,
  type LayoutEntry,
  type WidgetSize,
} from "./layout";
import { GettingStarted } from "./GettingStarted";
import { useDashboardLayout } from "./useDashboardLayout";
import { DashboardWidget } from "./widgets";

export { accountTotals, monthRange } from "./helpers";

function isSize(value: string): value is WidgetSize {
  return SIZE_OPTIONS.some((size) => size.value === value);
}

function WidgetControls({
  entry,
  index,
  count,
  layout,
  save,
}: {
  entry: LayoutEntry;
  index: number;
  count: number;
  layout: DashboardLayout;
  save: (next: DashboardLayout) => void;
}) {
  const title = widgetInfo(entry.widget).title;
  return (
    <div className="dash-controls" role="group" aria-label={`Arrange ${title}`}>
      <Button
        size="sm"
        disabled={index === 0}
        aria-label={`Move ${title} earlier`}
        onClick={() => {
          save(moveWidget(layout, entry.widget, -1));
        }}
      >
        Earlier
      </Button>
      <Button
        size="sm"
        disabled={index === count - 1}
        aria-label={`Move ${title} later`}
        onClick={() => {
          save(moveWidget(layout, entry.widget, 1));
        }}
      >
        Later
      </Button>
      <Select
        aria-label={`Size of ${title}`}
        value={entry.size}
        options={SIZE_OPTIONS}
        onChange={(event) => {
          const size = event.target.value;
          if (isSize(size)) {
            save(resizeWidget(layout, entry.widget, size));
          }
        }}
      />
      <Button
        size="sm"
        variant="danger"
        aria-label={`Remove ${title}`}
        onClick={() => {
          save(removeWidget(layout, entry.widget));
        }}
      >
        Remove
      </Button>
    </div>
  );
}

function Catalogue({ layout, save }: { layout: DashboardLayout; save: (next: DashboardLayout) => void }) {
  const available = availableWidgets(layout);
  return (
    <Card title="Add a widget" headingLevel={2}>
      {available.length === 0 ? (
        <p className="muted">Every widget is on the page.</p>
      ) : (
        <ul className="planning-list" aria-label="Widgets you can add">
          {available.map((info) => (
            <li key={info.id} className="planning-list__item">
              <span>
                <strong>{info.title}</strong>
                <br />
                <span className="muted">{info.description}</span>
              </span>
              <Button
                size="sm"
                aria-label={`Add ${info.title}`}
                onClick={() => {
                  save(addWidget(layout, info.id));
                }}
              >
                Add
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function DashboardPage() {
  const { layout, settled, ready, saving, save } = useDashboardLayout();
  const [editing, setEditing] = useState(false);
  const isEditing = editing && ready;

  return (
    <PlanningPage
      title="Dashboard"
      actions={
        <>
          {isEditing ? (
            <Button
              disabled={saving}
              onClick={() => {
                save(DEFAULT_LAYOUT);
              }}
            >
              Reset to default
            </Button>
          ) : null}
          <Button
            variant={isEditing ? "primary" : "secondary"}
            disabled={!ready}
            aria-pressed={isEditing}
            onClick={() => {
              setEditing((value) => !value);
            }}
          >
            {isEditing ? "Done" : "Customize"}
          </Button>
        </>
      }
    >
      {!settled ? <Spinner label="Loading dashboard" /> : null}
      {settled && !isEditing ? <GettingStarted /> : null}
      {settled && layout.length === 0 && !isEditing ? (
        <p className="muted">The dashboard is empty. Choose Customize to add widgets.</p>
      ) : null}
      <div className="dash-grid">
        {(settled ? layout : []).map((entry, index) => (
          <div key={entry.widget} className={cx("dash-slot", `dash-slot--${entry.size}`, isEditing && "dash-slot--editing")}>
            {isEditing ? <WidgetControls entry={entry} index={index} count={layout.length} layout={layout} save={save} /> : null}
            <DashboardWidget id={entry.widget} />
          </div>
        ))}
      </div>
      {isEditing ? <Catalogue layout={layout} save={save} /> : null}
    </PlanningPage>
  );
}
