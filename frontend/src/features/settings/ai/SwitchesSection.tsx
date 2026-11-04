// SPDX-License-Identifier: AGPL-3.0-or-later
/** Kill switch status: the household switch (owner) and the instance switch (instance admin). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Spinner } from "../../../components/Spinner";
import { useToast } from "../../../components/Toast";
import { authErrorMessage } from "../../auth/errors";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";
import { useAuthSession } from "../../auth/session";
import { aiKeys, getAccess, setHouseholdEnabled, setSystemEnabled } from "./api";

export function SwitchesSection() {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const runWithStepUp = useStepUp();
  const { session } = useAuthSession();
  const access = useQuery({ queryKey: aiKeys.access, queryFn: getAccess });
  const isAdmin = session?.user.is_instance_admin === true;

  const toggle = useMutation({
    mutationFn: ({ scope, enabled }: { scope: "household" | "system"; enabled: boolean }) =>
      runWithStepUp(() => (scope === "household" ? setHouseholdEnabled(enabled) : setSystemEnabled(enabled))),
    onSuccess: async (_data, { scope, enabled }) => {
      notify({
        tone: "success",
        title: `${scope === "household" ? "Household" : "Instance"} AI access ${enabled ? "on" : "off"}`,
      });
      await queryClient.invalidateQueries({ queryKey: aiKeys.access });
    },
    onError: (error) => {
      if (!isStepUpCancelled(error)) {
        notify({ tone: "error", title: "Could not change the switch", description: authErrorMessage(error) });
      }
    },
  });

  if (access.isPending) {
    return <Spinner label="Loading AI access status" />;
  }
  if (access.isError) {
    return (
      <p className="auth-error" role="alert">
        {authErrorMessage(access.error)}
      </p>
    );
  }
  const status = access.data;
  return (
    <Card title="Kill switch">
      <div className="settings-stack">
        <p role="status" aria-live="polite">
          {status.enabled ? (
            <>
              <Badge tone="warning">AI access is on</Badge> {String(status.active_clients)} active{" "}
              {status.active_clients === 1 ? "client" : "clients"} can read your financial facts.
            </>
          ) : (
            <>
              <Badge tone="success">AI access is off</Badge> Every AI call is refused, and no client can be created.
              Existing clients are kept and resume when it is switched back on.
            </>
          )}
        </p>
        <ul className="ai-switches">
          <li>
            <span>
              This household: <strong>{status.household_enabled ? "on" : "off"}</strong>
            </span>
            <Button
              variant={status.household_enabled ? "danger" : "primary"}
              loading={toggle.isPending && toggle.variables.scope === "household"}
              onClick={() => {
                toggle.mutate({ scope: "household", enabled: !status.household_enabled });
              }}
            >
              {status.household_enabled ? "Switch off for this household" : "Switch on for this household"}
            </Button>
          </li>
          <li>
            <span>
              Whole instance: <strong>{status.system_enabled ? "on" : "off"}</strong>
            </span>
            {isAdmin ? (
              <Button
                variant={status.system_enabled ? "danger" : "primary"}
                loading={toggle.isPending && toggle.variables.scope === "system"}
                onClick={() => {
                  toggle.mutate({ scope: "system", enabled: !status.system_enabled });
                }}
              >
                {status.system_enabled ? "Engage the instance kill switch" : "Release the instance kill switch"}
              </Button>
            ) : (
              <span className="auth-muted">Only an instance administrator can change this.</span>
            )}
          </li>
        </ul>
      </div>
    </Card>
  );
}
