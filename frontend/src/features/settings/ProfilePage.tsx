// SPDX-License-Identifier: AGPL-3.0-or-later
/** Profile (`GET/PATCH /settings/user`): display name, locale, theme, time zone. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { useTheme } from "../../app/ThemeProvider";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { queryRoots } from "../../lib/queryKeys";
import { authErrorMessage, fieldErrorsOf } from "../auth/errors";
import { getUserPreferences, settingsKeys, updateUserPreferences, type UserPreferences } from "./api";
import { SettingsPage } from "./SettingsLayout";

const THEMES = [
  { value: "system", label: "Follow the system" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
] as const;

type Theme = UserPreferences["theme"];

function isTheme(value: string): value is Theme {
  return THEMES.some((theme) => theme.value === value);
}

function ProfileForm({ initial }: { initial: UserPreferences }) {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const { setPreference } = useTheme();
  const [form, setForm] = useState(initial);
  const save = useMutation({
    mutationFn: updateUserPreferences,
    onSuccess: (saved) => {
      queryClient.setQueryData(settingsKeys.user, saved);
      setPreference(saved.theme);
      void queryClient.invalidateQueries({ queryKey: queryRoots.session });
      notify({ tone: "success", title: "Profile saved" });
    },
  });
  const fieldErrors = fieldErrorsOf(save.error);

  const onSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    save.mutate({
      display_name: form.display_name.trim(),
      locale: form.locale.trim(),
      theme: form.theme,
      timezone: form.timezone.trim(),
    });
  };

  return (
    <form className="auth-stack" noValidate onSubmit={onSubmit}>
      <Field label="Display name" error={fieldErrors["display_name"]} required>
        <Input
          name="display-name"
          autoComplete="name"
          maxLength={200}
          value={form.display_name}
          onChange={(event) => {
            setForm({ ...form, display_name: event.target.value });
          }}
        />
      </Field>
      <Field label="Language and region" hint="A language tag such as en-US or de-DE." error={fieldErrors["locale"]}>
        <Input
          name="locale"
          maxLength={35}
          value={form.locale}
          onChange={(event) => {
            setForm({ ...form, locale: event.target.value });
          }}
        />
      </Field>
      <Field label="Theme" error={fieldErrors["theme"]}>
        <Select
          name="theme"
          options={THEMES}
          value={form.theme}
          onChange={(event) => {
            const { value } = event.target;
            if (isTheme(value)) {
              setForm({ ...form, theme: value });
            }
          }}
        />
      </Field>
      <Field label="Time zone" hint="An IANA name such as Europe/Berlin or America/New_York." error={fieldErrors["timezone"]}>
        <Input
          name="timezone"
          maxLength={64}
          value={form.timezone}
          onChange={(event) => {
            setForm({ ...form, timezone: event.target.value });
          }}
        />
      </Field>
      {save.isError ? (
        <p className="auth-error" role="alert">
          {authErrorMessage(save.error)}
        </p>
      ) : null}
      <div className="auth-actions">
        <Button type="submit" variant="primary" loading={save.isPending}>
          Save profile
        </Button>
      </div>
    </form>
  );
}

export function ProfilePage() {
  const preferences = useQuery({ queryKey: settingsKeys.user, queryFn: getUserPreferences });
  return (
    <SettingsPage title="Profile">
      <Card title="Your profile">
        {preferences.isPending ? <Spinner label="Loading your profile" /> : null}
        {preferences.isError ? (
          <p className="auth-error" role="alert">
            {authErrorMessage(preferences.error)}
          </p>
        ) : null}
        {preferences.isSuccess ? <ProfileForm initial={preferences.data} /> : null}
      </Card>
    </SettingsPage>
  );
}
