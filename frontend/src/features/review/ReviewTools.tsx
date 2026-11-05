// SPDX-License-Identifier: AGPL-3.0-or-later
/** The review tools: page highlights, alternative readings, remember layout, rule draft, owner. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { useAccounts, accountLabel } from "../accounts/api";
import { useAuthSession } from "../auth/session";
import { PageViewer } from "../documents/PageViewer";
import { createRule } from "../rules/api";
import { listMembers, settingsKeys } from "../settings/api";
import { getTransaction, txKeys } from "../transactions/api";
import {
  fixFromAlternative,
  getReviewItem,
  getRuleDraft,
  institutionChoices,
  layoutRequest,
  lowConfidenceResolution,
  mergeFix,
  ownerResolution,
  rememberLayout,
  resolveItem,
  reviewKeys,
  type Alternative,
  type ResolveBody,
  type ReviewItem,
  type TransactionFix,
} from "./api";

/** The item with its regions and alternatives (the list omits them). */
export function useItemDetail(item: ReviewItem, enabled = true) {
  return useQuery({
    queryKey: reviewKeys.one(item.id),
    queryFn: () => getReviewItem(item.id),
    enabled,
  });
}

/** The page highlights of an item, or nothing when it has no document. */
export function ItemViewer({ item, detail }: { item: ReviewItem; detail: ReviewItem | undefined }) {
  if (item.document_id === null) {
    return null;
  }
  return <PageViewer documentId={item.document_id} regions={detail?.regions ?? []} />;
}

function rowIds(detail: ReviewItem): string[] {
  const ids = new Set<string>();
  for (const region of detail.regions ?? []) {
    if (region.transaction_id !== null && region.transaction_id !== undefined) {
      ids.add(region.transaction_id);
    }
  }
  for (const alt of detail.alternatives ?? []) {
    if (alt.transaction_id !== null && alt.transaction_id !== undefined) {
      ids.add(alt.transaction_id);
    }
  }
  return [...ids];
}

function useResolve(item: ReviewItem, title: string, onError: (message: string) => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (body: ResolveBody) => resolveItem(item.id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
      toast.notify({ title, tone: "success" });
    },
    onError: (caught) => {
      onError(errorMessage(caught));
    },
  });
}

function AlternativeButtons({
  alternatives,
  onPick,
}: {
  alternatives: readonly Alternative[];
  onPick: (alt: Alternative) => void;
}) {
  if (alternatives.length === 0) {
    return null;
  }
  return (
    <ul className="alternatives" aria-label="Other readings">
      {alternatives.map((alt) => (
        <li key={`${alt.field}-${alt.value}`}>
          <Button
            size="sm"
            onClick={() => {
              onPick(alt);
            }}
          >
            {`Use ${alt.field === "amount" ? "amount" : "date"} ${alt.value}`}
          </Button>
        </li>
      ))}
    </ul>
  );
}

function DoubtfulRow({
  id,
  alternatives,
  fix,
  onFix,
}: {
  id: string;
  alternatives: readonly Alternative[];
  fix: TransactionFix | undefined;
  onFix: (fix: TransactionFix) => void;
}) {
  const row = useQuery({ queryKey: txKeys.one(id), queryFn: () => getTransaction(id) });
  if (row.isPending) {
    return <Spinner label="Loading row" />;
  }
  if (row.isError) {
    return <p className="ledger-muted">A row is no longer available.</p>;
  }
  const tx = row.data;
  return (
    <div className="ledger-stack" role="group" aria-label={`Row ${tx.raw_payee ?? tx.id}`}>
      <p>
        {fix?.posted_date ?? tx.posted_date} {tx.raw_payee ?? "-"}{" "}
        <MoneyText
          amount={fix?.amount === undefined || fix.amount === null ? tx.amount.amount : String(fix.amount.amount)}
          currency={tx.amount.currency}
        />
        {fix === undefined ? "" : " (corrected)"}
      </p>
      <AlternativeButtons
        alternatives={alternatives}
        onPick={(alt) => {
          const made = fixFromAlternative(alt, tx);
          if (made !== null) {
            onFix(made);
          }
        }}
      />
    </div>
  );
}

/** Doubtful rows: highlighted on the page, with other readings to pick from. */
export function LowConfidenceTools({ item, actionable }: { item: ReviewItem; actionable: boolean }) {
  const detail = useItemDetail(item);
  const [fixes, setFixes] = useState<TransactionFix[]>([]);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, "Rows confirmed", setError);
  if (detail.isPending) {
    return <Spinner label="Loading the page" />;
  }
  if (detail.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(detail.error)}
      </p>
    );
  }
  const ids = rowIds(detail.data);
  return (
    <div className="ledger-stack">
      <p>PerFin was not sure it read these rows correctly. Check them against the page.</p>
      <ItemViewer item={item} detail={detail.data} />
      {ids.map((id) => (
        <DoubtfulRow
          key={id}
          id={id}
          alternatives={(detail.data.alternatives ?? []).filter((alt) => alt.transaction_id === id)}
          fix={fixes.find((f) => f.transaction_id === id)}
          onFix={(fix) => {
            setFixes((previous) => mergeFix(previous, fix));
          }}
        />
      ))}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
      {actionable ? (
        <div className="ledger-actions">
          <Button
            variant="primary"
            loading={resolve.isPending}
            onClick={() => {
              resolve.mutate(lowConfidenceResolution(fixes, true));
            }}
          >
            {fixes.length === 0 ? "Confirm as read" : "Save corrections and confirm"}
          </Button>
          <RuleDraftButton item={item} />
        </div>
      ) : null}
    </div>
  );
}

/** "Create a rule from this": the server drafts it from the item's first row, one click saves it. */
export function RuleDraftButton({ item }: { item: ReviewItem }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [savedId, setSavedId] = useState<string | null>(null);
  const draft = useMutation({ mutationFn: () => getRuleDraft(item.id), onError: (c) => { setError(errorMessage(c)); } });
  const save = useMutation({
    mutationFn: () => {
      if (draft.data === undefined) {
        throw new Error("no draft");
      }
      return createRule(draft.data.rule);
    },
    onSuccess: (rule) => {
      setSavedId(rule.id);
      void queryClient.invalidateQueries({ queryKey: queryRoots.rules });
    },
    onError: (c) => {
      setError(errorMessage(c));
    },
  });
  if (savedId !== null) {
    return (
      <Link to="/rules/$ruleId" params={{ ruleId: savedId }} className="link-action">
        Rule saved. Open it
      </Link>
    );
  }
  if (draft.data === undefined) {
    return (
      <>
        <Button
          loading={draft.isPending}
          onClick={() => {
            setError(null);
            draft.mutate();
          }}
        >
          Create a rule from this
        </Button>
        {error === null ? null : <span role="alert" className="ledger-error">{error}</span>}
      </>
    );
  }
  return (
    <span className="ledger-actions" role="group" aria-label="Rule draft">
      <span>Draft rule: {draft.data.rule.name}</span>
      <Button variant="primary" loading={save.isPending} onClick={() => { save.mutate(); }}>
        Save this rule
      </Button>
      {error === null ? null : <span role="alert" className="ledger-error">{error}</span>}
    </span>
  );
}

/** A new institution: remember the layout so the next statement of it is read the same way. */
export function NewInstitutionTools({ item, actionable }: { item: ReviewItem; actionable: boolean }) {
  const queryClient = useQueryClient();
  const detail = useItemDetail(item);
  const [institution, setInstitution] = useState("");
  const [similar, setSimilar] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const remember = useMutation({
    mutationFn: () => rememberLayout(item.id, layoutRequest(institution, similar)),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  if (detail.isPending) {
    return <Spinner label="Loading the page" />;
  }
  if (detail.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(detail.error)}
      </p>
    );
  }
  const choices = institutionChoices(detail.data);
  const out = remember.data;
  return (
    <div className="ledger-stack">
      <p>PerFin does not know this layout yet. Tell it which institution the file is from and it reads the next one the same way.</p>
      <ItemViewer item={item} detail={detail.data} />
      {actionable ? (
        <>
          <Field label="Institution">
            <Select
              placeholder="Choose an institution"
              options={choices.map((choice) => ({ value: choice.id, label: choice.label }))}
              value={institution}
              onChange={(event) => {
                setInstitution(event.target.value);
              }}
            />
          </Field>
          <Checkbox
            label="Read files already received with this layout again"
            checked={similar}
            onChange={(event) => {
              setSimilar(event.target.checked);
            }}
          />
          <div className="ledger-actions">
            <Button
              variant="primary"
              disabled={institution === ""}
              loading={remember.isPending}
              onClick={() => {
                remember.mutate();
              }}
            >
              Remember this layout
            </Button>
          </div>
        </>
      ) : null}
      {out === undefined ? null : (
        <p role="status">
          Layout remembered. {out.reprocessed.length} file(s) are being read again.
          {out.ambiguous ? " The layout also matches other institutions, so check the next import." : ""}
        </p>
      )}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** A drop-folder file nobody could be found for: choose the member (and account). */
export function UnknownOwnerTools({ item, actionable }: { item: ReviewItem; actionable: boolean }) {
  const { session } = useAuthSession();
  const householdId = session?.household?.id ?? "";
  const members = useQuery({
    queryKey: settingsKeys.members(householdId),
    queryFn: () => listMembers(householdId),
    enabled: householdId !== "" && actionable,
  });
  const accounts = useAccounts();
  const [userId, setUserId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, "File assigned", setError);
  return (
    <div className="ledger-stack">
      <p>
        A file arrived in the drop folder and PerFin could not tell whose it is. It is held until you choose.
      </p>
      {item.document_id === null ? null : (
        <Link to="/documents/$documentId" params={{ documentId: item.document_id }} className="link-action">
          Look at the file
        </Link>
      )}
      {actionable ? (
        <>
          <Field label="Whose file is it?">
            <Select
              placeholder="Choose a member"
              options={(members.data ?? []).filter((m) => m.is_active).map((m) => ({ value: m.user_id, label: m.display_name }))}
              value={userId}
              onChange={(event) => {
                setUserId(event.target.value);
              }}
            />
          </Field>
          <Field label="Account (optional)">
            <Select
              placeholder="Let PerFin match the account"
              options={(accounts.data ?? [])
                .filter((a) => a.can_edit && !a.is_closed)
                .map((a) => ({ value: a.id, label: accountLabel(a) }))}
              value={accountId}
              onChange={(event) => {
                setAccountId(event.target.value);
              }}
            />
          </Field>
          <Checkbox
            label="Send files from this subfolder to this member from now on"
            checked={remember}
            onChange={(event) => {
              setRemember(event.target.checked);
            }}
          />
          <div className="ledger-actions">
            <Button
              variant="primary"
              disabled={userId === ""}
              loading={resolve.isPending}
              onClick={() => {
                resolve.mutate(ownerResolution(userId, accountId, remember));
              }}
            >
              Assign this file
            </Button>
          </div>
        </>
      ) : null}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** The receipt itself, shown on request next to the suggested transactions. */
export function ReceiptPreview({ item }: { item: ReviewItem }) {
  const [shown, setShown] = useState(false);
  if (item.document_id === null) {
    return null;
  }
  return (
    <div className="ledger-stack">
      <Button
        size="sm"
        onClick={() => {
          setShown(!shown);
        }}
      >
        {shown ? "Hide receipt" : "Show receipt"}
      </Button>
      {shown ? <PageViewer documentId={item.document_id} /> : null}
    </div>
  );
}
