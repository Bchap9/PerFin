// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Categories (hierarchy editor), payees (with merge and aliases) and tags.
 * Writes are household-wide; the server decides who may (member and above).
 */
import { useMemo, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { Tabs } from "../../components/Tabs";
import { SettingsPage } from "../settings/SettingsLayout";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { referenceKeys, useCategories, usePayees, useTags, nameLookup, type Category, type Payee, type Tag } from "../planning/queries";
import { useAction } from "../planning/useAction";
import {
  createCategory,
  createPayee,
  createTag,
  deleteCategory,
  deletePayee,
  deleteTag,
  mergeCategory,
  mergePayee,
  renameTag,
  updateCategory,
  updatePayee,
  type CategoryUpdate,
} from "./api";
import { MergeDialog } from "./MergeDialog";
import { categoryOptions, descendantIds, flattenCategories } from "./tree";

type Kind = Category["kind"];
type Tax = NonNullable<Category["tax_treatment"]>;

const KINDS: readonly { value: Kind; label: string }[] = [
  { value: "expense", label: "Expense" },
  { value: "income", label: "Income" },
  { value: "transfer", label: "Transfer" },
  { value: "ignore", label: "Ignored" },
];

const TAX: readonly { value: Tax; label: string }[] = [
  { value: "taxable_income", label: "Taxable income" },
  { value: "tax_exempt_income", label: "Tax-exempt income" },
  { value: "deductible", label: "Deductible" },
  { value: "tax_payment", label: "Tax payment" },
];

const taxonomy = [referenceKeys.categories, referenceKeys.payees, referenceKeys.tags] as const;

function isKind(value: string): value is Kind {
  return KINDS.some((kind) => kind.value === value);
}

function taxValue(value: string): Tax | null {
  return TAX.find((tax) => tax.value === value)?.value ?? null;
}

// ---- categories --------------------------------------------------------------

function CategoryDialog({
  category,
  categories,
  onClose,
}: {
  category: Category | null;
  categories: readonly Category[];
  onClose: () => void;
}) {
  const [name, setName] = useState(category?.name ?? "");
  const [parentId, setParentId] = useState(category?.parent_id ?? "");
  const [kind, setKind] = useState<Kind>(category?.kind ?? "expense");
  const [tax, setTax] = useState<string>(category?.tax_treatment ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const blocked = category === null ? new Set<string>() : descendantIds(categories, category.id);
  const parents = categoryOptions(categories, (candidate) => !blocked.has(candidate.id));
  const save = useAction(
    (body: CategoryUpdate) => (category === null ? createCategory({ ...body, name: body.name ?? "" }) : updateCategory(category.id, body)),
    { invalidate: taxonomy, success: category === null ? "Category added" : "Category saved" },
  );
  const submit = async (): Promise<void> => {
    if (name.trim() === "") {
      setProblem("Enter a name.");
      return;
    }
    setProblem(null);
    const done = await save.run({
      name: name.trim(),
      parent_id: parentId === "" ? null : parentId,
      kind,
      tax_treatment: taxValue(tax),
    });
    if (done !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={category === null ? "Add category" : "Edit category"}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Name" error={problem} required>
          <Input
            value={name}
            maxLength={100}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <Field label="Parent category">
          <Select
            value={parentId}
            placeholder="No parent (top level)"
            options={parents}
            onChange={(event) => {
              setParentId(event.target.value);
            }}
          />
        </Field>
        <Field label="Kind">
          <Select
            value={kind}
            options={KINDS}
            onChange={(event) => {
              if (isKind(event.target.value)) {
                setKind(event.target.value);
              }
            }}
          />
        </Field>
        <Field label="Tax treatment">
          <Select
            value={tax}
            placeholder="None"
            options={TAX}
            onChange={(event) => {
              setTax(event.target.value);
            }}
          />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

function ConfirmDelete({
  noun,
  name,
  remove,
  onClose,
}: {
  noun: string;
  name: string;
  remove: () => Promise<unknown>;
  onClose: () => void;
}) {
  const action = useAction(remove, { invalidate: taxonomy, success: `${name} deleted` });
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Delete ${noun}`}
      description={`Delete "${name}"? Transactions that use it become uncategorised or untagged.`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={action.busy}
            onClick={() => {
              void action.run().then((done) => {
                if (done !== undefined) {
                  onClose();
                }
              });
            }}
          >
            Delete
          </Button>
        </>
      }
    >
      <ErrorLine message={action.error} />
    </Dialog>
  );
}

function CategoriesPanel() {
  const query = useCategories();
  const [editing, setEditing] = useState<Category | "new" | null>(null);
  const [merging, setMerging] = useState<Category | null>(null);
  const [deleting, setDeleting] = useState<Category | null>(null);
  const categories = useMemo(() => query.data ?? [], [query.data]);
  const nodes = useMemo(() => flattenCategories(categories), [categories]);
  const columns: TableColumn<(typeof nodes)[number]>[] = [
    {
      id: "name",
      header: "Category",
      rowHeader: true,
      cell: ({ category, depth }) => (
        <span style={{ paddingInlineStart: `${String(depth * 1.25)}rem` }}>{category.name}</span>
      ),
    },
    { id: "kind", header: "Kind", cell: ({ category }) => <Badge>{KINDS.find((k) => k.value === category.kind)?.label ?? category.kind}</Badge> },
    {
      id: "tax",
      header: "Tax",
      cell: ({ category }) => TAX.find((t) => t.value === category.tax_treatment)?.label ?? <span className="muted">-</span>,
    },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: ({ category }) => (
        <span className="planning-actions">
          <Button size="sm" aria-label={`Edit ${category.name}`} onClick={() => { setEditing(category); }}>
            Edit
          </Button>
          <Button size="sm" aria-label={`Merge ${category.name}`} onClick={() => { setMerging(category); }}>
            Merge
          </Button>
          {category.system_key === null ? (
            <Button size="sm" variant="danger" aria-label={`Delete ${category.name}`} onClick={() => { setDeleting(category); }}>
              Delete
            </Button>
          ) : null}
        </span>
      ),
    },
  ];
  return (
    <QueryState loading={query.isPending} error={query.error} label="categories">
      <div className="planning-actions">
        <Button
          variant="primary"
          onClick={() => {
            setEditing("new");
          }}
        >
          Add category
        </Button>
      </div>
      <Table
        caption="Categories"
        captionHidden
        columns={columns}
        rows={nodes}
        rowKey={(node) => node.category.id}
        maxHeight="none"
        empty={<EmptyState title="No categories yet" headingLevel={3} />}
      />
      {editing === null ? null : (
        <CategoryDialog category={editing === "new" ? null : editing} categories={categories} onClose={() => { setEditing(null); }} />
      )}
      {merging === null ? null : (
        <MergeDialog
          noun="category"
          subjectName={merging.name}
          options={categoryOptions(categories, (c) => c.id !== merging.id)}
          merge={(into) => mergeCategory(merging.id, into)}
          invalidate={taxonomy}
          onClose={() => { setMerging(null); }}
        />
      )}
      {deleting === null ? null : (
        <ConfirmDelete noun="category" name={deleting.name} remove={() => deleteCategory(deleting.id)} onClose={() => { setDeleting(null); }} />
      )}
    </QueryState>
  );
}

// ---- payees ------------------------------------------------------------------

function parseAliases(text: string): string[] {
  return [...new Set(text.split(",").map((alias) => alias.trim()).filter((alias) => alias !== ""))];
}

function PayeeDialog({ payee, categories, onClose }: { payee: Payee | null; categories: readonly Category[]; onClose: () => void }) {
  const [name, setName] = useState(payee?.name ?? "");
  const [aliases, setAliases] = useState(payee?.aliases.join(", ") ?? "");
  const [defaultCategory, setDefaultCategory] = useState(payee?.default_category_id ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const save = useAction(
    (body: { name: string; aliases: string[]; default_category_id: string | null }) =>
      payee === null ? createPayee(body) : updatePayee(payee.id, body),
    { invalidate: taxonomy, success: payee === null ? "Payee added" : "Payee saved" },
  );
  const submit = async (): Promise<void> => {
    if (name.trim() === "") {
      setProblem("Enter a name.");
      return;
    }
    setProblem(null);
    const done = await save.run({
      name: name.trim(),
      aliases: parseAliases(aliases),
      default_category_id: defaultCategory === "" ? null : defaultCategory,
    });
    if (done !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={payee === null ? "Add payee" : "Edit payee"}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Name" error={problem} required>
          <Input value={name} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Field label="Aliases" hint="Other spellings seen on statements, separated by commas.">
          <Input value={aliases} onChange={(event) => { setAliases(event.target.value); }} />
        </Field>
        <Field label="Default category">
          <Select
            value={defaultCategory}
            placeholder="None"
            options={categoryOptions(categories)}
            onChange={(event) => { setDefaultCategory(event.target.value); }}
          />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

function PayeesPanel() {
  const query = usePayees();
  const categoriesQuery = useCategories();
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<Payee | "new" | null>(null);
  const [merging, setMerging] = useState<Payee | null>(null);
  const [deleting, setDeleting] = useState<Payee | null>(null);
  const categories = useMemo(() => categoriesQuery.data ?? [], [categoriesQuery.data]);
  const categoryName = nameLookup(categories);
  const needle = filter.trim().toLowerCase();
  const rows = (query.data ?? []).filter(
    (payee) => needle === "" || payee.name.toLowerCase().includes(needle) || payee.aliases.some((a) => a.toLowerCase().includes(needle)),
  );
  const columns: TableColumn<Payee>[] = [
    { id: "name", header: "Payee", rowHeader: true, cell: (payee) => payee.name },
    { id: "aliases", header: "Aliases", cell: (payee) => payee.aliases.join(", ") || <span className="muted">-</span> },
    {
      id: "category",
      header: "Default category",
      cell: (payee) => (payee.default_category_id === null ? <span className="muted">-</span> : categoryName(payee.default_category_id)),
    },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (payee) => (
        <span className="planning-actions">
          <Button size="sm" aria-label={`Edit ${payee.name}`} onClick={() => { setEditing(payee); }}>Edit</Button>
          <Button size="sm" aria-label={`Merge ${payee.name}`} onClick={() => { setMerging(payee); }}>Merge</Button>
          <Button size="sm" variant="danger" aria-label={`Delete ${payee.name}`} onClick={() => { setDeleting(payee); }}>Delete</Button>
        </span>
      ),
    },
  ];
  return (
    <QueryState loading={query.isPending} error={query.error} label="payees">
      <div className="planning-row">
        <Field label="Search payees">
          <Input type="search" value={filter} onChange={(event) => { setFilter(event.target.value); }} />
        </Field>
        <Button variant="primary" onClick={() => { setEditing("new"); }}>
          Add payee
        </Button>
      </div>
      <Table
        caption="Payees"
        captionHidden
        columns={columns}
        rows={rows}
        rowKey={(payee) => payee.id}
        maxHeight="none"
        empty={<EmptyState title="No payees" headingLevel={3} />}
      />
      {editing === null ? null : (
        <PayeeDialog payee={editing === "new" ? null : editing} categories={categories} onClose={() => { setEditing(null); }} />
      )}
      {merging === null ? null : (
        <MergeDialog
          noun="payee"
          subjectName={merging.name}
          options={(query.data ?? []).filter((p) => p.id !== merging.id).map((p) => ({ value: p.id, label: p.name }))}
          merge={(into) => mergePayee(merging.id, into)}
          invalidate={taxonomy}
          onClose={() => { setMerging(null); }}
        />
      )}
      {deleting === null ? null : (
        <ConfirmDelete noun="payee" name={deleting.name} remove={() => deletePayee(deleting.id)} onClose={() => { setDeleting(null); }} />
      )}
    </QueryState>
  );
}

// ---- tags --------------------------------------------------------------------

function TagsPanel() {
  const query = useTags();
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<Tag | null>(null);
  const [newName, setNewName] = useState("");
  const [deleting, setDeleting] = useState<Tag | null>(null);
  const add = useAction(createTag, { invalidate: [referenceKeys.tags], success: "Tag added" });
  const rename = useAction((tag: Tag, to: string) => renameTag(tag.id, to), { invalidate: [referenceKeys.tags], success: "Tag renamed" });
  return (
    <QueryState loading={query.isPending} error={query.error} label="tags">
      <form
        className="planning-row"
        aria-label="Add a tag"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim() !== "") {
            void add.run(name.trim()).then((done) => {
              if (done !== undefined) {
                setName("");
              }
            });
          }
        }}
      >
        <Field label="New tag">
          <Input value={name} maxLength={50} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Button type="submit" variant="primary" loading={add.busy}>
          Add tag
        </Button>
      </form>
      <ErrorLine message={add.error} />
      {(query.data ?? []).length === 0 ? (
        <EmptyState title="No tags yet" headingLevel={3} />
      ) : (
        <ul className="planning-list" aria-label="Tags">
          {(query.data ?? []).map((tag) => (
            <li key={tag.id} className="planning-list__item">
              <Badge tone="info">{tag.name}</Badge>
              <span className="planning-actions">
                <Button
                  size="sm"
                  aria-label={`Rename ${tag.name}`}
                  onClick={() => {
                    setRenaming(tag);
                    setNewName(tag.name);
                  }}
                >
                  Rename
                </Button>
                <Button size="sm" variant="danger" aria-label={`Delete ${tag.name}`} onClick={() => { setDeleting(tag); }}>
                  Delete
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
      {renaming === null ? null : (
        <Dialog
          open
          onClose={() => { setRenaming(null); }}
          title="Rename tag"
          footer={
            <>
              <Button onClick={() => { setRenaming(null); }}>Cancel</Button>
              <Button
                variant="primary"
                loading={rename.busy}
                onClick={() => {
                  void rename.run(renaming, newName.trim()).then((done) => {
                    if (done !== undefined) {
                      setRenaming(null);
                    }
                  });
                }}
              >
                Save
              </Button>
            </>
          }
        >
          <Field label="Tag name">
            <Input value={newName} onChange={(event) => { setNewName(event.target.value); }} />
          </Field>
          <ErrorLine message={rename.error} />
        </Dialog>
      )}
      {deleting === null ? null : (
        <ConfirmDelete noun="tag" name={deleting.name} remove={() => deleteTag(deleting.id)} onClose={() => { setDeleting(null); }} />
      )}
    </QueryState>
  );
}

export function CategoriesPage() {
  return (
    <SettingsPage title="Categories, payees and tags">
      <Tabs
        label="Organise"
        tabs={[
          { id: "categories", label: "Categories", content: <CategoriesPanel /> },
          { id: "payees", label: "Payees", content: <PayeesPanel /> },
          { id: "tags", label: "Tags", content: <TagsPanel /> },
        ]}
      />
    </SettingsPage>
  );
}
