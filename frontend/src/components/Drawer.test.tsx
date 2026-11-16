// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";

import { expectNoAxeViolations } from "../test/axe";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Drawer } from "./Drawer";

function Harness({ onClose = () => undefined }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true);
        }}
      >
        Open details
      </Button>
      <Drawer
        open={open}
        title="Coffee Corner"
        description="Card ending 0000, 3 March"
        onClose={() => {
          onClose();
          setOpen(false);
        }}
        footer={<Button variant="primary">Save</Button>}
      >
        <label>
          Note
          <input type="text" />
        </label>
      </Drawer>
    </>
  );
}

describe("Drawer", () => {
  it("opens as a labelled dialog and traps focus", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open details" }));

    const drawer = screen.getByRole("dialog", { name: "Coffee Corner" });
    expect(drawer.tagName).toBe("DIALOG");
    expect(drawer).toHaveClass("drawer");
    expect(drawer).toHaveAccessibleDescription("Card ending 0000, 3 March");

    const close = within(drawer).getByRole("button", { name: "Close" });
    const save = within(drawer).getByRole("button", { name: "Save" });
    // Focus moves into the drawer when it opens.
    expect(drawer.contains(document.activeElement)).toBe(true);

    // Tab from the last control wraps to the first; Shift+Tab from the first wraps to the last.
    save.focus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(save).toHaveFocus();
  });

  it("Escape closes and focus returns to the opener", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const opener = screen.getByRole("button", { name: "Open details" });
    await user.click(opener);
    screen.getByRole("textbox", { name: "Note" }).focus();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("puts the initial focus where asked", async () => {
    const user = userEvent.setup();
    function WithInitial() {
      const [open, setOpen] = useState(false);
      const target = useRef<HTMLInputElement>(null);
      return (
        <>
          <Button
            onClick={() => {
              setOpen(true);
            }}
          >
            Open
          </Button>
          <Drawer open={open} title="Edit" onClose={() => { setOpen(false); }} initialFocus={target}>
            <label>
              First
              <input type="text" />
            </label>
            <label>
              Second
              <input ref={target} type="text" />
            </label>
          </Drawer>
        </>
      );
    }
    render(<WithInitial />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("textbox", { name: "Second" })).toHaveFocus();
  });

  it("Escape in a dialog opened from the drawer closes only that dialog", async () => {
    const user = userEvent.setup();
    const drawerClose = vi.fn();
    const dialogClose = vi.fn();
    render(
      <Drawer open title="Coffee Corner" onClose={drawerClose}>
        <Dialog open title="New category" onClose={dialogClose}>
          <label>
            Name
            <input type="text" />
          </label>
        </Dialog>
      </Drawer>,
    );
    screen.getByRole("textbox", { name: "Name" }).focus();
    await user.keyboard("{Escape}");
    expect(drawerClose).not.toHaveBeenCalled();
  });

  it("has no axe violations", async () => {
    const user = userEvent.setup();
    const { container } = render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open details" }));
    await expectNoAxeViolations(container);
  });
});
