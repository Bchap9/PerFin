// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Make a category where the person is working (the transaction drawer, a
 * rule, a budget), without leaving for Settings: a name and, optionally, the
 * top-level category it goes inside (a top-level one gets the server's
 * default kind; one inside a parent takes the parent's). The new category is
 * handed back through `onCreated`, so the picker that opened the dialog can
 * select it, and then the dialog closes (`onClose`). A
 * validation problem from the server is shown under the field it is about.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { errorMessage, isApiError } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useCategories, type Category } from "../planning/queries";
import { createCategory, type CategoryCreate } from "./api";

export interface NewCategoryDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (category: Category) => void;
  /** The top-level category to start inside. */
  parentId?: string;
}

interface Problems {
  name?: string;
  parent?: string;
  other?: string;
}

function problemsOf(error: unknown): Problems {
  if (!isApiError(error)) {
    return { other: errorMessage(error) };
  }
  const fields = new Map(Object.entries(error.fieldErrors()));
  const name = fields.get("name");
  const parent = fields.get("parent_id");
  if (name !== undefined || parent !== undefined) {
    return { ...(name === undefined ? {} : { name }), ...(parent === undefined ? {} : { parent }) };
  }
  // A 422 without field details (a name already taken, a kind mismatch) is about the name the person typed.
  return error.status === 422 ? { name: errorMessage(error) } : { other: errorMessage(error) };
}

/** Mounted only while open, so each opening starts empty, inside `parentId`. */
export function NewCategoryDialog(props: NewCategoryDialogProps) {
  return props.open ? <NewCategoryForm {...props} /> : null;
}

function NewCategoryForm({ onClose, onCreated, parentId }: NewCategoryDialogProps) {
  const queryClient = useQueryClient();
  const categories = useCategories();
  const [name, setName] = useState("");
  const [parent, setParent] = useState(parentId ?? "");
  const [problems, setProblems] = useState<Problems>({});
  const formId = useId();

  const create = useMutation({
    mutationFn: (body: CategoryCreate) => createCategory(body),
    onSuccess: async (category) => {
      await queryClient.invalidateQueries({ queryKey: queryRoots.taxonomy });
      setName("");
      setProblems({});
      onCreated(category);
      onClose();
    },
    onError: (error) => {
      setProblems(problemsOf(error));
    },
  });

  const topLevel = (categories.data ?? [])
    .filter((category) => category.parent_id === null)
    .sort((a, b) => a.name.localeCompare(b.name));

  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    // Mounted inside another form (a rule editor), the submit stays here.
    event?.stopPropagation();
    const trimmed = name.trim();
    if (trimmed === "") {
      setProblems({ name: "Enter a name." });
      return;
    }
    setProblems({});
    create.mutate({ name: trimmed, ...(parent === "" ? {} : { parent_id: parent }) });
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="New category"
      size="sm"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form={formId} loading={create.isPending}>
            Create category
          </Button>
        </>
      }
    >
      <form id={formId} noValidate onSubmit={submit}>
        <Field label="Name" required error={problems.name}>
          <Input
            value={name}
            autoComplete="off"
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <Field
          label="Inside"
          hint={categories.isError ? "Could not load the categories; this one will be top-level." : undefined}
          error={problems.parent}
        >
          <Select
            value={parent}
            placeholder="Nothing (a top-level category)"
            options={topLevel.map((category) => ({ value: category.id, label: category.name }))}
            onChange={(event) => {
              setParent(event.target.value);
            }}
          />
        </Field>
      </form>
      <ErrorLine message={problems.other ?? null} />
    </Dialog>
  );
}
