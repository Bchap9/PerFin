// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Category to schedule/line hints for the accountant. A hint, not tax advice.
 * The "Tax hints" tab of Settings, Categories (`?tab=tax`), so it has no page
 * header of its own. Saving and removing a hint is a member's; a viewer reads
 * the hints.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Input } from "../../components/Input";
import { Table, type TableColumn } from "../../components/Table";
import { queryRoots } from "../../lib/queryKeys";
import { useCanWrite } from "../accounts/api";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useCategories, type Category } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { deleteCategoryMap, listCategoryMap, putCategoryMap, taxKeys, type CategoryMapEntry } from "./api";

const JURISDICTION = "US";

function MapRow({ category, entry }: { category: Category; entry: CategoryMapEntry | undefined }) {
  const [schedule, setSchedule] = useState(entry?.schedule ?? "");
  const [line, setLine] = useState(entry?.line ?? "");
  const invalidate = [queryRoots.tax];
  const save = useAction(
    () =>
      putCategoryMap(category.id, {
        jurisdiction: JURISDICTION,
        schedule: schedule.trim(),
        line: line.trim() === "" ? null : line.trim(),
      }),
    { invalidate, success: `Hint saved for ${category.name}` },
  );
  const clear = useAction(() => deleteCategoryMap(category.id, JURISDICTION), {
    invalidate,
    success: `Hint removed for ${category.name}`,
  });
  const dirty = schedule.trim() !== (entry?.schedule ?? "") || line.trim() !== (entry?.line ?? "");
  return (
    <div className="planning-row">
      <Input
        aria-label={`Schedule for ${category.name}`}
        placeholder="Schedule, e.g. Schedule A"
        value={schedule}
        onChange={(event) => {
          setSchedule(event.target.value);
        }}
      />
      <Input
        aria-label={`Line for ${category.name}`}
        placeholder="Line (optional)"
        value={line}
        onChange={(event) => {
          setLine(event.target.value);
        }}
      />
      <Button
        size="sm"
        variant="primary"
        aria-label={`Save hint for ${category.name}`}
        disabled={!dirty || schedule.trim() === ""}
        loading={save.busy}
        onClick={() => void save.run()}
      >
        Save
      </Button>
      {entry === undefined ? null : (
        <Button
          size="sm"
          variant="danger"
          aria-label={`Remove hint for ${category.name}`}
          loading={clear.busy}
          onClick={() => void clear.run()}
        >
          Remove
        </Button>
      )}
      <ErrorLine message={save.error ?? clear.error} />
    </div>
  );
}

function hintText(entry: CategoryMapEntry | undefined): string {
  if (entry === undefined) {
    return "No hint";
  }
  return entry.line === null ? entry.schedule : `${entry.schedule}, line ${entry.line}`;
}

export function CategoryMapPage() {
  const canWrite = useCanWrite();
  const categories = useCategories();
  const map = useQuery({ queryKey: taxKeys.map(JURISDICTION), queryFn: () => listCategoryMap(JURISDICTION) });
  const entries = new Map((map.data ?? []).map((entry) => [entry.category_id, entry]));
  const rows = [...(categories.data ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const columns: TableColumn<Category>[] = [
    { id: "category", header: "Category", rowHeader: true, cell: (row) => row.name },
    { id: "treatment", header: "Tax treatment", cell: (row) => row.tax_treatment?.replaceAll("_", " ") ?? "none" },
    {
      id: "hint",
      header: "Schedule and line",
      cell: (row) => {
        const entry = entries.get(row.id);
        if (!canWrite) {
          return entry === undefined ? <span className="muted">{hintText(entry)}</span> : hintText(entry);
        }
        return <MapRow key={`${row.id}:${entry?.schedule ?? ""}:${entry?.line ?? ""}`} category={row} entry={entry} />;
      },
    },
  ];
  return (
    <Card title="Schedule hints" headingLevel={3}>
      <p>
        Maps a category to the schedule and line your accountant files it under. It is a hint for the roll-up and the export
        of a <Link to="/reports/tax">tax year</Link>, not tax advice.
      </p>
      <QueryState loading={categories.isPending || map.isPending} error={categories.error ?? map.error} label="schedule hints">
        <Table caption="Schedule hints by category" captionHidden columns={columns} rows={rows} rowKey={(row) => row.id} />
      </QueryState>
    </Card>
  );
}
