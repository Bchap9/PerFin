// SPDX-License-Identifier: AGPL-3.0-or-later
/** The saved dashboard layout (`dashboard_layout` of `/settings/user`) and a saver. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { getUserPreferences, settingsKeys, updateUserPreferences, type UserPreferences } from "../settings/api";
import { effectiveLayout, type DashboardLayout } from "./layout";

export interface DashboardLayoutState {
  layout: DashboardLayout;
  /** True once the saved layout is known (loaded, or could not be read and the default applies). */
  settled: boolean;
  /** False while loading and when the preferences could not be read: editing would overwrite unseen state. */
  ready: boolean;
  saving: boolean;
  save: (next: DashboardLayout) => void;
}

export function useDashboardLayout(): DashboardLayoutState {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const query = useQuery({ queryKey: settingsKeys.user, queryFn: getUserPreferences });
  const mutation = useMutation({
    mutationFn: (layout: DashboardLayout) => updateUserPreferences({ dashboard_layout: layout }),
    onMutate: async (layout) => {
      await queryClient.cancelQueries({ queryKey: settingsKeys.user });
      const previous = queryClient.getQueryData<UserPreferences>(settingsKeys.user);
      if (previous !== undefined) {
        queryClient.setQueryData<UserPreferences>(settingsKeys.user, { ...previous, dashboard_layout: layout });
      }
      return { previous };
    },
    onError: (error, _layout, context) => {
      if (context?.previous !== undefined) {
        queryClient.setQueryData(settingsKeys.user, context.previous);
      }
      notify({ tone: "error", title: "Could not save the dashboard layout", description: errorMessage(error) });
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(settingsKeys.user, saved);
    },
  });
  return {
    layout: effectiveLayout(query.data?.dashboard_layout),
    settled: !query.isPending,
    ready: query.data !== undefined,
    saving: mutation.isPending,
    save: (next) => {
      mutation.mutate(next);
    },
  };
}
