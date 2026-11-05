// SPDX-License-Identifier: AGPL-3.0-or-later
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate } from "../../lib/dates";
import { formatFraction } from "../investments/format";
import type { ScheduleRow } from "./api";

const money = (m: { amount: string; currency: string }) => <MoneyText amount={m.amount} currency={m.currency} tone="none" />;

/** An amortisation schedule: one row per payment. */
export function ScheduleTable({ rows, caption }: { rows: readonly ScheduleRow[]; caption: string }) {
  const columns: TableColumn<ScheduleRow>[] = [
    { id: "n", header: "#", align: "end", rowHeader: true, cell: (row) => String(row.number) },
    { id: "due", header: "Due", cell: (row) => formatDate(row.due_date) ?? row.due_date },
    { id: "rate", header: "Rate", align: "end", cell: (row) => formatFraction(row.rate, 3) },
    { id: "payment", header: "Payment", align: "end", cell: (row) => money(row.payment) },
    { id: "principal", header: "Principal", align: "end", cell: (row) => money(row.principal) },
    { id: "interest", header: "Interest", align: "end", cell: (row) => money(row.interest) },
    { id: "extra", header: "Extra", align: "end", cell: (row) => money(row.extra) },
    { id: "escrow", header: "Escrow", align: "end", cell: (row) => money(row.escrow) },
    { id: "balance", header: "Balance", align: "end", cell: (row) => money(row.balance) },
  ];
  return <Table caption={caption} captionHidden columns={columns} rows={rows} rowKey={(row) => row.number} density="compact" empty="No payments left." />;
}
