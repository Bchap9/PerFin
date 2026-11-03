// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Second factors (`GET /auth/mfa`): list, add (authenticator app, security
 * key or passkey), remove (step-up), and regenerate recovery codes
 * (step-up; all earlier codes stop working, the new ones are shown once).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Dialog } from "../../../components/Dialog";
import { Spinner } from "../../../components/Spinner";
import { Table, type TableColumn } from "../../../components/Table";
import { useToast } from "../../../components/Toast";
import { queryRoots } from "../../../lib/queryKeys";
import {
  getMfaStatus,
  regenerateRecoveryCodes,
  removeTotp,
  removeWebauthn,
  type FactorAdded,
  type MfaMethod,
} from "../../auth/api";
import { authErrorMessage } from "../../auth/errors";
import { TotpSetup } from "../../auth/mfa/TotpSetup";
import { WebAuthnSetup } from "../../auth/mfa/WebAuthnSetup";
import { ShowOnceDialog } from "../../auth/ShowOnceDialog";
import { isStepUpCancelled, mfaStatusKey, useStepUp } from "../../auth/StepUp";
import { when } from "../SettingsLayout";

const TYPE_LABELS: Readonly<Record<MfaMethod["type"], string>> = {
  totp: "Authenticator app",
  webauthn: "Security key or passkey",
};

type Confirm = { kind: "remove"; method: MfaMethod } | { kind: "regenerate" };

export function MfaSection() {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const status = useQuery({ queryKey: mfaStatusKey, queryFn: getMfaStatus });
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = (): Promise<void> => queryClient.invalidateQueries({ queryKey: queryRoots.session });

  const onAdded = (added: FactorAdded): void => {
    void refresh();
    notify({ tone: "success", title: `${added.method.name} added` });
    if (added.recovery_codes !== undefined && added.recovery_codes !== null && added.recovery_codes.length > 0) {
      setCodes(added.recovery_codes);
    }
  };

  const runConfirmed = async (): Promise<void> => {
    if (confirm === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (confirm.kind === "remove") {
        const { method } = confirm;
        await runWithStepUp(() => (method.type === "totp" ? removeTotp() : removeWebauthn(method.id)));
        notify({ tone: "success", title: `${method.name} removed` });
      } else {
        const fresh = await runWithStepUp(regenerateRecoveryCodes);
        setCodes(fresh);
      }
      setConfirm(null);
      await refresh();
    } catch (caught) {
      if (isStepUpCancelled(caught)) {
        setConfirm(null);
      } else {
        setError(authErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  const columns: readonly TableColumn<MfaMethod>[] = [
    { id: "name", header: "Name", cell: (method) => method.name, rowHeader: true },
    { id: "type", header: "Type", cell: (method) => TYPE_LABELS[method.type] },
    { id: "added", header: "Added", cell: (method) => when(method.created_at) },
    { id: "used", header: "Last used", cell: (method) => when(method.last_used_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (method) => (
        <Button
          size="sm"
          variant="danger"
          aria-label={`Remove ${method.name}`}
          onClick={() => {
            setError(null);
            setConfirm({ kind: "remove", method });
          }}
        >
          Remove
        </Button>
      ),
    },
  ];

  const methods = status.data?.methods ?? [];
  const hasTotp = methods.some((method) => method.type === "totp");

  return (
    <Card title="Second factors">
      {status.isPending ? <Spinner label="Loading your second factors" /> : null}
      {status.isError ? (
        <p className="auth-error" role="alert">
          {authErrorMessage(status.error)}
        </p>
      ) : null}
      {status.isSuccess ? (
        <div className="settings-stack">
          <p>
            {status.data.required
              ? "This server requires a second factor for every account."
              : "A second factor protects your account if your password leaks."}
          </p>
          <Table
            caption="Your second factors"
            captionHidden
            columns={columns}
            rows={methods}
            rowKey={(method) => method.id}
            empty="No second factor yet."
            maxHeight="none"
          />
          <h3 className="auth-subheading">Add a security key or passkey</h3>
          <WebAuthnSetup onAdded={onAdded} />
          {hasTotp ? null : (
            <>
              <h3 className="auth-subheading">Add an authenticator app</h3>
              <TotpSetup onAdded={onAdded} />
            </>
          )}
          <h3 className="auth-subheading">Recovery codes</h3>
          <p>
            <Badge tone={status.data.recovery_codes_remaining > 2 ? "success" : "warning"}>
              {`${String(status.data.recovery_codes_remaining)} unused`}
            </Badge>{" "}
            Each code signs you in once if you lose your second factor.
          </p>
          <div className="auth-actions">
            <Button
              disabled={methods.length === 0}
              onClick={() => {
                setError(null);
                setConfirm({ kind: "regenerate" });
              }}
            >
              Generate new recovery codes
            </Button>
          </div>
        </div>
      ) : null}
      <Dialog
        open={confirm !== null}
        size="sm"
        onClose={() => {
          setConfirm(null);
        }}
        title={confirm?.kind === "remove" ? `Remove ${confirm.method.name}?` : "Generate new recovery codes?"}
        description={
          confirm?.kind === "remove"
            ? "You will no longer be able to sign in with it."
            : "Every earlier recovery code stops working."
        }
        footer={
          <>
            <Button
              onClick={() => {
                setConfirm(null);
              }}
            >
              Cancel
            </Button>
            <Button
              variant={confirm?.kind === "remove" ? "danger" : "primary"}
              loading={busy}
              onClick={() => {
                void runConfirmed();
              }}
            >
              {confirm?.kind === "remove" ? "Remove" : "Generate"}
            </Button>
          </>
        }
      >
        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
      </Dialog>
      <ShowOnceDialog
        open={codes !== null}
        title="Your new recovery codes"
        description="Store them somewhere safe, apart from your password. They are shown only now."
        lines={codes ?? []}
        label="recovery codes"
        onDone={() => {
          setCodes(null);
        }}
      />
    </Card>
  );
}
