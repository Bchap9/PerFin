// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Categories, Payees and Tags tabs of Settings, Categories (the page and
 * its tab bar are `CategoriesSettingsPage`). Writes are household-wide, and
 * only the buttons the caller's role may use are shown, as the API decides
 * them: a member adds, edits and renames; merging and deleting are an
 * owner's or admin's; a viewer sees the lists only.
 */
import "./categories.css";

import { useMemo, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { canWriteLedger } from "../accounts/api";
import { canManageMembers, useAuthSession } from "../auth/session";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { referenceKeys, useCategories, usePayees, useTags, nameLookup, type Category, type Payee, type Tag } from "../planning/queries";
import { useAction } from "../planning/useAction";
import {
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
import { NewCategoryDialog } from "./NewCategoryDialog";
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

/** What the caller's household role lets them change here (`authz_policy.py`). */
export interface TaxonomyRights {
  /** Add, edit and rename: member and above. */
  write: boolean;
  /** Merge and delete: owner and admin. */
  manage: boolean;
}

export function useTaxonomyRights(): TaxonomyRights {
  const { session } = useAuthSession();
  const role = session?.household?.role;
  return { write: canWriteLedger(role), manage: canManageMembers(role) };
}

/** The tree indent class for a depth; deeper than three keeps the third step. */
function indentClass(depth: number): string | undefined {
  return depth <= 0 ? undefined : `indent-${String(Math.min(depth, 3))}`;
}

function isKind(value: string): value is Kind {
  return KINDS.some((kind) => kind.value === value);
}

function taxValue(value: string): Tax | null {
  return TAX.find((tax) => tax.value === value)?.value ?? null;
}

// ---- categories --------------------------------------------------------------

/** Edit a category: name, parent (any depth), kind and tax treatment. A new one is made with `NewCategoryDialog`. */
function CategoryDialog({
  category,
  categories,
  onClose,
}: {
  category: Category;
  categories: readonly Category[];
  onClose: () => void;
}) {
  const [name, setName] = useState(category.name);
  const [parentId, setParentId] = useState(category.parent_id ?? "");
  const [kind, setKind] = useState<Kind>(category.kind);
  const [tax, setTax] = useState<string>(category.tax_treatment ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const blocked = descendantIds(categories, category.id);
  const parents = categoryOptions(categories, (candidate) => !blocked.has(candidate.id));
  const save = useAction((body: CategoryUpdate) => updateCategory(category.id, body), {
    invalidate: taxonomy,
    success: "Category saved",
  });
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
      title="Edit category"
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

export function CategoriesPanel() {
  const query = useCategories();
  const rights = useTaxonomyRights();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Category | null>(null);
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
        <span className={indentClass(depth)}>{category.name}</span>
      ),
    },
    { id: "kind", header: "Kind", cell: ({ category }) => <Badge>{KINDS.find((k) => k.value === category.kind)?.label ?? category.kind}</Badge> },
    {
      id: "tax",
      header: "Tax",
      cell: ({ category }) => TAX.find((t) => t.value === category.tax_treatment)?.label ?? <span className="muted">-</span>,
    },
  ];
  if (rights.write) {
    columns.push({
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: ({ category }) => (
        <span className="planning-actions">
          <Button size="sm" aria-label={`Edit ${category.name}`} onClick={() => { setEditing(category); }}>
            Edit
          </Button>
          {rights.manage ? (
            <Button size="sm" aria-label={`Merge ${category.name}`} onClick={() => { setMerging(category); }}>
              Merge
            </Button>
          ) : null}
          {rights.manage && category.system_key === null ? (
            <Button size="sm" variant="danger" aria-label={`Delete ${category.name}`} onClick={() => { setDeleting(category); }}>
              Delete
            </Button>
          ) : null}
        </span>
      ),
    });
  }
  return (
    <QueryState loading={query.isPending} error={query.error} label="categories">
      {rights.write ? (
        <div className="planning-actions">
          <Button
            variant="primary"
            onClick={() => {
              setCreating(true);
            }}
          >
            New category
          </Button>
        </div>
      ) : null}
      <Table
        caption="Categories"
        captionHidden
        columns={columns}
        rows={nodes}
        rowKey={(node) => node.category.id}
        maxHeight="none"
        empty={<EmptyState title="No categories yet" headingLevel={3} />}
      />
      <NewCategoryDialog
        open={creating}
        onClose={() => {
          setCreating(false);
        }}
        onCreated={() => undefined}
      />
      {editing === null ? null : (
        <CategoryDialog category={editing} categories={categories} onClose={() => { setEditing(null); }} />
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

export function PayeesPanel() {
  const query = usePayees();
  const rights = useTaxonomyRights();
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
  ];
  if (rights.write) {
    columns.push({
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (payee) => (
        <span className="planning-actions">
          <Button size="sm" aria-label={`Edit ${payee.name}`} onClick={() => { setEditing(payee); }}>Edit</Button>
          {rights.manage ? (
            <>
              <Button size="sm" aria-label={`Merge ${payee.name}`} onClick={() => { setMerging(payee); }}>Merge</Button>
              <Button size="sm" variant="danger" aria-label={`Delete ${payee.name}`} onClick={() => { setDeleting(payee); }}>Delete</Button>
            </>
          ) : null}
        </span>
      ),
    });
  }
  return (
    <QueryState loading={query.isPending} error={query.error} label="payees">
      <div className="planning-row">
        <Field label="Search payees">
          <Input type="search" value={filter} onChange={(event) => { setFilter(event.target.value); }} />
        </Field>
        {rights.write ? (
          <Button variant="primary" onClick={() => { setEditing("new"); }}>
            Add payee
          </Button>
        ) : null}
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

export function TagsPanel() {
  const query = useTags();
  const rights = useTaxonomyRights();
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<Tag | null>(null);
  const [newName, setNewName] = useState("");
  const [deleting, setDeleting] = useState<Tag | null>(null);
  const add = useAction(createTag, { invalidate: [referenceKeys.tags], success: "Tag added" });
  const rename = useAction((tag: Tag, to: string) => renameTag(tag.id, to), { invalidate: [referenceKeys.tags], success: "Tag renamed" });
  return (
    <QueryState loading={query.isPending} error={query.error} label="tags">
      {rights.write ? (
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
      ) : null}
      <ErrorLine message={add.error} />
      {(query.data ?? []).length === 0 ? (
        <EmptyState title="No tags yet" headingLevel={3} />
      ) : (
        <ul className="planning-list" aria-label="Tags">
          {(query.data ?? []).map((tag) => (
            <li key={tag.id} className="planning-list__item">
              <Badge tone="info">{tag.name}</Badge>
              {rights.write ? (
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
                  {rights.manage ? (
                    <Button size="sm" variant="danger" aria-label={`Delete ${tag.name}`} onClick={() => { setDeleting(tag); }}>
                      Delete
                    </Button>
                  ) : null}
                </span>
              ) : null}
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
