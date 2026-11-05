// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import {
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  EmptyState,
  Field,
  IconButton,
  Input,
  MoneyText,
  Select,
  Spinner,
  Table,
  Tabs,
  ToastProvider,
  useToast,
} from ".";
import { icons } from ".";

describe("design system smoke test", () => {
  it("renders every component accessibly", () => {
    render(
      <Card title="Accounts" actions={<Badge tone="success">Synced</Badge>} footer="Footer">
        <Button variant="primary">Save</Button>
        <Button loading>Saving</Button>
        <IconButton label="Close panel" icon={<icons.CloseIcon />} />
        <Spinner label="Loading balances" />
        <EmptyState title="No accounts yet" description="Add one to get started." headingLevel={3} />
        <Field label="Name" hint="Shown in lists" error="Required" required>
          <Input />
        </Field>
        <Field label="Kind">
          <Select placeholder="Choose" options={[{ value: "checking", label: "Checking" }]} />
        </Field>
        <Checkbox label="Hidden from household" hint="Only you can see it" />
      </Card>,
    );
    expect(screen.getByRole("region", { name: "Accounts" })).toBeInTheDocument();
    expect(screen.getByText("Synced")).toHaveClass("badge", "badge--success");
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
    const busy = screen.getByRole("button", { name: "Saving" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Close panel" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading balances");
    expect(screen.getByRole("heading", { level: 3, name: "No accounts yet" })).toBeInTheDocument();

    const name = screen.getByRole("textbox", { name: /Name/ });
    expect(name).toBeRequired();
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription("Shown in lists Required");
    expect(screen.getByRole("combobox", { name: "Kind" })).toHaveDisplayValue("Choose");
    expect(screen.getByRole("checkbox", { name: "Hidden from household" })).toHaveAccessibleDescription(
      "Only you can see it",
    );
  });

  it("renders money from strings exactly, with a machine-readable value", () => {
    const { container } = render(
      <p>
        <MoneyText amount="-1234567890123.45" currency="USD" locale="en-US" />
        <MoneyText amount="10" currency="EUR" locale="en-US" tone="signed" />
        <MoneyText amount="garbage" currency="USD" />
      </p>,
    );
    const negative = screen.getByText("-$1,234,567,890,123.45");
    expect(negative.tagName).toBe("DATA");
    expect(negative).toHaveAttribute("value", "-1234567890123.45");
    expect(negative).toHaveClass("money--negative");
    expect(screen.getByText("€10.00")).toHaveClass("money--positive");
    expect(screen.getByText("Amount unavailable")).toBeInTheDocument();
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });

  it("renders a table with a labelled, focusable scroll region and an empty state", () => {
    const rows = [
      { id: "a", name: "Checking", balance: "12.50" },
      { id: "b", name: "Savings", balance: "-3.00" },
    ];
    const columns = [
      { id: "name", header: "Account", cell: (row: (typeof rows)[number]) => row.name, rowHeader: true },
      {
        id: "balance",
        header: "Balance",
        align: "end" as const,
        cell: (row: (typeof rows)[number]) => <MoneyText amount={row.balance} currency="USD" locale="en-US" />,
      },
    ];
    const { rerender } = render(<Table caption="Balances" columns={columns} rows={rows} rowKey={(row) => row.id} />);
    const region = screen.getByRole("region", { name: "Balances" });
    expect(region).toHaveAttribute("tabindex", "0");
    const table = within(region).getByRole("table", { name: "Balances" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Account", "Balance"]);
    expect(within(table).getByRole("rowheader", { name: "Savings" })).toBeInTheDocument();
    expect(within(table).getByText("-$3.00")).toBeInTheDocument();

    rerender(<Table caption="Balances" columns={columns} rows={[]} rowKey={(row) => row.id} empty="No accounts" />);
    expect(screen.getByRole("cell", { name: "No accounts" })).toHaveAttribute("colspan", "2");
  });

  it("supports keyboard navigation in tabs", async () => {
    const user = userEvent.setup();
    render(
      <Tabs
        label="Account views"
        tabs={[
          { id: "overview", label: "Overview", content: "Overview panel" },
          { id: "disabled", label: "Disabled", content: "never", disabled: true },
          { id: "history", label: "History", content: "History panel" },
        ]}
      />,
    );
    const overview = screen.getByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Overview" })).toHaveTextContent("Overview panel");
    await user.click(overview);
    await user.keyboard("{ArrowRight}");
    const history = screen.getByRole("tab", { name: "History" });
    expect(history).toHaveAttribute("aria-selected", "true");
    expect(history).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "History" })).toHaveTextContent("History panel");
    await user.keyboard("{Home}");
    expect(overview).toHaveFocus();
    await user.keyboard("{End}");
    expect(history).toHaveFocus();
  });

  it("opens a <dialog>, closes on the close button and on Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <Button
            onClick={() => {
              setOpen(true);
            }}
          >
            Open
          </Button>
          <Dialog
            open={open}
            title="Delete account"
            description="This cannot be undone."
            onClose={() => {
              onClose();
              setOpen(false);
            }}
            footer={<Button variant="danger">Delete</Button>}
          >
            Body text
          </Dialog>
        </>
      );
    }
    render(<Harness />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open" }));
    const dialog = screen.getByRole("dialog", { name: "Delete account" });
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveAttribute("open");
    expect(dialog).toHaveAccessibleDescription("This cannot be undone.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open" }));
    const reopened = screen.getByRole("dialog", { name: "Delete account" });
    act(() => {
      reopened.dispatchEvent(new Event("cancel", { cancelable: true }));
    });
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows toasts in live regions and auto-dismisses non-errors", () => {
    vi.useFakeTimers();
    try {
      function Notifier() {
        const { notify } = useToast();
        return (
          <>
            <Button onClick={() => notify({ title: "Saved", tone: "success" })}>Notify</Button>
            <Button onClick={() => notify({ title: "Import failed", tone: "error" })}>Fail</Button>
          </>
        );
      }
      render(
        <ToastProvider>
          <Notifier />
        </ToastProvider>,
      );
      act(() => {
        screen.getByRole("button", { name: "Notify" }).click();
        screen.getByRole("button", { name: "Fail" }).click();
      });
      const region = screen.getByRole("region", { name: "Notifications" });
      expect(within(region).getByText("Saved")).toBeInTheDocument();
      expect(within(screen.getByRole("alert")).getByText("Import failed")).toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(5_000);
      });
      expect(screen.queryByText("Saved")).not.toBeInTheDocument();
      expect(screen.getByText("Import failed")).toBeInTheDocument();
      act(() => {
        within(screen.getByRole("alert")).getByRole("button", { name: "Dismiss notification" }).click();
      });
      expect(screen.queryByText("Import failed")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Table on narrow screens", () => {
  it("labels each cell for the card layout, and leaves action columns unlabelled", () => {
    interface R {
      id: string;
      name: string;
    }
    const columns = [
      { id: "name", header: "Name", rowHeader: true, cell: (row: R) => row.name },
      { id: "kind", header: "Kind of thing", label: "Kind", cell: () => "x" },
      { id: "actions", header: <span className="visually-hidden">Actions</span>, cell: () => <button type="button">Go</button> },
    ];
    const { container } = render(<Table caption="T" columns={columns} rows={[{ id: "1", name: "A" }]} rowKey={(row) => row.id} />);
    expect(container.querySelector("tbody th")).toHaveAttribute("data-label", "Name");
    const cells = container.querySelectorAll("tbody td");
    expect(cells[0]).toHaveAttribute("data-label", "Kind");
    expect(cells[1]).not.toHaveAttribute("data-label");
  });
});
