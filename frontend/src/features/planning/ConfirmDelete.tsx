// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { ErrorLine } from "./PlanningPage";

/**
 * Asks before a row is deleted. `confirm` deletes (a `useAction` run); the
 * dialog closes once it has, and shows a refusal otherwise. The row's own
 * button, which would take focus back, is then gone: its table takes it.
 */
export function ConfirmDelete({
  title,
  description,
  label,
  action,
  confirm,
  onClose,
}: {
  title: string;
  description: string;
  label: string;
  action: { busy: boolean; error: string | null };
  confirm: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [region] = useState(() => document.activeElement?.closest<HTMLElement>("[role=region]"));
  return (
    <Dialog
      open
      size="sm"
      onClose={onClose}
      title={title}
      description={description}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={action.busy}
            onClick={() => {
              void confirm().then((done) => {
                if (done !== undefined) {
                  onClose();
                  setTimeout(() => region?.focus());
                }
              });
            }}
          >
            {label}
          </Button>
        </>
      }
    >
      <ErrorLine message={action.error} />
    </Dialog>
  );
}
