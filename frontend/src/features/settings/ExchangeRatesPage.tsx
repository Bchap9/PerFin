// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Exchange rates (`/fx-rates`, docs/API.md "FX rates notes"): the rates net
 * worth and reports convert other currencies with. Everyone in the household
 * can read them; an owner or admin enters a rate for a pair and day and
 * deletes a rate that was entered by hand.
 *
 * How the server uses them (backend `app.domain.fx.service`): an amount is
 * converted with the latest rate on or before its date, read in either
 * direction, never through a third currency. A rate somebody entered wins
 * over a fetched one for its day and is never replaced by a fetch.
 */
import "./fx.css";

import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { todayIsoDate } from "../../lib/dates";
import { canManageMembers, useAuthSession } from "../auth/session";
import { useHousehold } from "../planning/currency";
import { ErrorLine } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { buildFilter, buildRate, dayLabel, inverseInWords, listInWords, rateInWords, serverFieldErrors, type RateForm } from "./fx";
import { deleteFxRate, FX_DEPENDENTS, fxKeys, listFxRates, MANUAL_SOURCE, NO_PAIR, setFxRate, type FxPairFilter, type FxRate } from "./fxApi";
import { useIdempotencyKey } from "../../lib/idempotency";
import { SettingsPage, when } from "./SettingsLayout";

function pairLabel(rate: { base: string; quote: string }): string {
  return `${rate.base} to ${rate.quote}`;
}

function AddRateForm({ baseCurrency, foreign }: { baseCurrency: string; foreign: readonly string[] }) {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const keys = useIdempotencyKey();
  const today = todayIsoDate();
  const [form, setForm] = useState<RateForm>({ base: foreign[0] ?? "", quote: baseCurrency, date: today, rate: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<RateForm>): void => {
    setForm({ ...form, ...patch });
  };
  const preview = buildRate(form, today).body;
  const inverse = preview === null ? null : inverseInWords(preview);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const built = buildRate(form, today);
    setErrors(built.errors);
    if (built.body === null) {
      return;
    }
    const body = built.body;
    setBusy(true);
    setFailure(null);
    try {
      const saved = await setFxRate(body, keys.keyFor(JSON.stringify(body)));
      keys.done();
      notify({ tone: "success", title: "Rate saved", description: `${rateInWords(saved)} from ${dayLabel(saved.rate_date)}.` });
      setForm({ ...form, rate: "" });
      await Promise.all(FX_DEPENDENTS.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    } catch (caught) {
      const fields = serverFieldErrors(caught);
      setErrors(fields);
      // A field message is shown at its field; anything else here.
      setFailure(Object.keys(fields).length > 0 ? null : errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="planning-form"
      noValidate
      aria-label="Add a rate"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <div className="fx-fields">
        <Field label="Currency" hint="The currency you hold, such as EUR." error={errors["base"]} required>
          <Input
            name="fx-base"
            maxLength={3}
            autoCapitalize="characters"
            autoComplete="off"
            spellCheck={false}
            value={form.base}
            onChange={(event) => {
              set({ base: event.target.value.toUpperCase() });
            }}
          />
        </Field>
        <Field label="Priced in" hint="Normally your base currency." error={errors["quote"]} required>
          <Input
            name="fx-quote"
            maxLength={3}
            autoCapitalize="characters"
            autoComplete="off"
            spellCheck={false}
            value={form.quote}
            onChange={(event) => {
              set({ quote: event.target.value.toUpperCase() });
            }}
          />
        </Field>
        <Field label="Date" hint="The first day the rate applies." error={errors["rate_date"]} required>
          <Input
            name="fx-date"
            type="date"
            max={today}
            value={form.date}
            onChange={(event) => {
              set({ date: event.target.value });
            }}
          />
        </Field>
        <Field label="Rate" hint="What one unit is worth, such as 1.0812." error={errors["rate"]} required>
          <Input
            name="fx-rate"
            inputMode="decimal"
            autoComplete="off"
            value={form.rate}
            onChange={(event) => {
              set({ rate: event.target.value });
            }}
          />
        </Field>
      </div>
      <p aria-live="polite" data-testid="rate-in-words">
        {preview === null ? (
          <span className="muted">Fill in the four fields to read the rate back in words before you save it.</span>
        ) : (
          <>
            <strong>{rateInWords(preview)}</strong> from {dayLabel(preview.rate_date)}
            {inverse === null ? "" : ` (${inverse})`}. It replaces any rate this pair already has on that day.
          </>
        )}
      </p>
      <ErrorLine message={failure} />
      <div>
        <Button type="submit" variant="primary" loading={busy}>
          Save rate
        </Button>
      </div>
    </form>
  );
}

function PairFilter({ applied, onApply }: { applied: FxPairFilter; onApply: (pair: FxPairFilter) => void }) {
  const [draft, setDraft] = useState<FxPairFilter>(applied);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const filtered = applied.base !== "" || applied.quote !== "";
  return (
    <form
      className="fx-filter"
      role="search"
      aria-label="Filter rates by pair"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        const built = buildFilter(draft);
        setErrors(built.ok ? {} : built.errors);
        if (built.ok) {
          setDraft(built.pair);
          onApply(built.pair);
        }
      }}
    >
      <Field label="Show currency" error={errors["base"]}>
        <Input
          name="fx-filter-base"
          maxLength={3}
          autoComplete="off"
          spellCheck={false}
          placeholder="Any"
          value={draft.base}
          onChange={(event) => {
            setDraft({ ...draft, base: event.target.value.toUpperCase() });
          }}
        />
      </Field>
      <Field label="Show priced in" error={errors["quote"]}>
        <Input
          name="fx-filter-quote"
          maxLength={3}
          autoComplete="off"
          spellCheck={false}
          placeholder="Any"
          value={draft.quote}
          onChange={(event) => {
            setDraft({ ...draft, quote: event.target.value.toUpperCase() });
          }}
        />
      </Field>
      <Button type="submit">Filter</Button>
      {filtered ? (
        <Button
          variant="ghost"
          onClick={() => {
            setDraft(NO_PAIR);
            setErrors({});
            onApply(NO_PAIR);
          }}
        >
          Show all pairs
        </Button>
      ) : null}
    </form>
  );
}

function DeleteRateDialog({ rate, onClose }: { rate: FxRate; onClose: () => void }) {
  const remove = useAction(deleteFxRate, { invalidate: FX_DEPENDENTS, success: "Rate deleted" });
  return (
    <Dialog
      open
      size="sm"
      onClose={onClose}
      title="Delete this rate?"
      description={`${rateInWords(rate)} from ${dayLabel(rate.rate_date)}.`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={remove.busy}
            onClick={() => {
              void remove.run(rate.id).then((done) => {
                if (done !== undefined) {
                  onClose();
                }
              });
            }}
          >
            Delete rate
          </Button>
        </>
      }
    >
      <p>
        Amounts dated {dayLabel(rate.rate_date)} or later are then converted with the rate before it. If there is none, they
        are left out of converted totals and shown as missing a rate.
      </p>
      <ErrorLine message={remove.error} />
    </Dialog>
  );
}

function RatesCard({ canWrite }: { canWrite: boolean }) {
  const [pair, setPair] = useState<FxPairFilter>(NO_PAIR);
  const [deleting, setDeleting] = useState<FxRate | null>(null);
  const query = useInfiniteQuery({
    queryKey: fxKeys.list(pair),
    queryFn: ({ pageParam }) => listFxRates(pair, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered = pair.base !== "" || pair.quote !== "";

  const columns: TableColumn<FxRate>[] = [
    { id: "date", header: "From", rowHeader: true, cell: (rate) => dayLabel(rate.rate_date) },
    { id: "pair", header: "Pair", cell: pairLabel },
    { id: "rate", header: "Rate", cell: rateInWords },
    {
      id: "source",
      header: "Source",
      cell: (rate) =>
        rate.source === MANUAL_SOURCE ? <Badge tone="info">Entered by hand</Badge> : <Badge tone="neutral">Fetched ({rate.source})</Badge>,
    },
    { id: "updated", header: "Recorded", cell: (rate) => when(rate.updated_at) },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: <span className="visually-hidden">Actions</span>,
            align: "end",
            cell: (rate) =>
              rate.source === MANUAL_SOURCE ? (
                <Button
                  size="sm"
                  variant="danger"
                  aria-label={`Delete the ${pairLabel(rate)} rate from ${dayLabel(rate.rate_date)}`}
                  onClick={() => {
                    setDeleting(rate);
                  }}
                >
                  Delete
                </Button>
              ) : null,
          } satisfies TableColumn<FxRate>,
        ]
      : []),
  ];

  return (
    <Card title="Recorded rates">
      <div className="planning-form">
        <PairFilter applied={pair} onApply={setPair} />
        <p className="muted">
          Newest first. A pair is listed the way it was recorded, so EUR to USD and USD to EUR are separate; PerFin reads
          either one when it converts.
        </p>
        {query.isPending ? <Spinner label="Loading exchange rates" /> : null}
        {query.isError ? <ErrorLine message={errorMessage(query.error)} /> : null}
        {query.isSuccess ? (
          <Table
            caption={
              filtered
                ? `Recorded exchange rates, ${pair.base === "" ? "any currency" : pair.base} to ${pair.quote === "" ? "any currency" : pair.quote}`
                : "Recorded exchange rates"
            }
            captionHidden
            columns={columns}
            rows={rows}
            rowKey={(rate) => rate.id}
            maxHeight="none"
            empty={
              <EmptyState
                headingLevel={3}
                title={filtered ? "No rates for this pair" : "No exchange rates yet"}
                description={
                  filtered
                    ? "Try the pair the other way round, or show all pairs."
                    : canWrite
                      ? "Add one above for each currency you hold other than your base currency."
                      : "An owner or admin can add them."
                }
              />
            }
          />
        ) : null}
        {query.hasNextPage ? (
          <div>
            <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
              Show older rates
            </Button>
          </div>
        ) : null}
      </div>
      {deleting === null ? null : (
        <DeleteRateDialog
          rate={deleting}
          onClose={() => {
            setDeleting(null);
          }}
        />
      )}
    </Card>
  );
}

export function ExchangeRatesPage() {
  const { session } = useAuthSession();
  const household = session?.household ?? null;
  const details = useHousehold();
  const accounts = useAccounts();
  const baseCurrency = details.data?.base_currency ?? null;
  const foreign = useMemo(
    () => [...new Set((accounts.data ?? []).map((account) => account.currency))].filter((code) => code !== baseCurrency).sort(),
    [accounts.data, baseCurrency],
  );

  if (session === null) {
    return (
      <SettingsPage title="Exchange rates">
        <Spinner label="Loading" />
      </SettingsPage>
    );
  }
  if (household === null) {
    return (
      <SettingsPage title="Exchange rates">
        <Card>
          <p>You are not a member of a household yet. Ask an administrator to add you to one.</p>
        </Card>
      </SettingsPage>
    );
  }
  const canWrite = canManageMembers(household.role);
  const ready = baseCurrency !== null && !accounts.isPending;

  return (
    <SettingsPage title="Exchange rates">
      <Card title="How rates are used">
        <div className="planning-form">
          <p>
            Net worth and reports add everything up in your base currency
            {baseCurrency === null ? "" : ` (${baseCurrency})`}. An amount in another currency is converted with the latest
            rate on or before its own date, so one rate keeps applying until you record a newer one. An amount with no rate on
            or before its date is left out of the total and shown as missing a rate.
          </p>
          <p>
            Record each currency against your base currency: PerFin does not convert through a third currency. If this server
            fetches rates automatically they appear below with their source. A rate you enter always wins for its day and is
            never overwritten by a fetched one; to correct a fetched rate, enter your own for the same day.
          </p>
          {baseCurrency !== null && foreign.length > 0 ? (
            <p role="note">
              Your accounts hold {listInWords(foreign)} besides {baseCurrency}, so {foreign.length === 1 ? "that currency needs" : "each of those needs"}{" "}
              a rate to {baseCurrency}.
            </p>
          ) : null}
        </div>
      </Card>
      {canWrite ? (
        <Card title="Add a rate">
          {details.isError ? <ErrorLine message={errorMessage(details.error)} /> : null}
          {ready ? (
            <AddRateForm key={baseCurrency} baseCurrency={baseCurrency} foreign={foreign} />
          ) : details.isError ? null : (
            <Spinner label="Loading your currencies" />
          )}
        </Card>
      ) : (
        <Card>
          <p>
            Only a household owner or administrator can add or delete exchange rates, because a rate changes every converted
            total in the household.
          </p>
        </Card>
      )}
      <RatesCard canWrite={canWrite} />
    </SettingsPage>
  );
}
