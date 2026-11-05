// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Select, type SelectOption } from "../../components/Select";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";

/** "Merge X into Y": everything pointing at X moves to Y, then X is removed. */
export function MergeDialog({
  noun,
  subjectName,
  options,
  merge,
  invalidate,
  onClose,
}: {
  noun: string;
  subjectName: string;
  options: readonly SelectOption[];
  merge: (into: string) => Promise<unknown>;
  invalidate: readonly (readonly string[])[];
  onClose: () => void;
}) {
  const [target, setTarget] = useState("");
  const action = useAction(merge, { invalidate, success: `${subjectName} merged` });
  const submit = async (): Promise<void> => {
    if ((await action.run(target)) !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Merge ${noun}`}
      description={`Move everything from "${subjectName}" to another ${noun}, then remove "${subjectName}". This cannot be undone.`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={target === ""} loading={action.busy} onClick={() => void submit()}>
            Merge
          </Button>
        </>
      }
    >
      <Field label={`Merge into`}>
        <Select
          value={target}
          placeholder={`Choose a ${noun}`}
          options={options}
          onChange={(event) => {
            setTarget(event.target.value);
          }}
        />
      </Field>
      <ErrorLine message={action.error} />
    </Dialog>
  );
}
