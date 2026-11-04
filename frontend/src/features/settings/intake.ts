// SPDX-License-Identifier: AGPL-3.0-or-later
/** Pure helpers of the intake settings page: form state, the update body and test outcomes. */
import type { MailIntake, MailIntakeUpdate, MailTest } from "./api";

export interface IntakeForm {
  enabled: boolean;
  host: string;
  port: string;
  username: string;
  /** Write-only: blank keeps the stored password. */
  password: string;
  inboxFolder: string;
  processedFolder: string;
  rejectedFolder: string;
  requireAuthResults: boolean;
  authservId: string;
  memberAddresses: boolean;
  senders: { address: string; userId: string }[];
}

export function formFrom(intake: MailIntake): IntakeForm {
  return {
    enabled: intake.enabled,
    host: intake.host ?? "",
    port: String(intake.port),
    username: intake.username ?? "",
    password: "",
    inboxFolder: intake.inbox_folder,
    processedFolder: intake.processed_folder,
    rejectedFolder: intake.rejected_folder,
    requireAuthResults: intake.require_auth_results,
    authservId: intake.authserv_id ?? "",
    memberAddresses: intake.member_addresses,
    senders: intake.allowed_senders.map((s) => ({ address: s.address, userId: s.user_id })),
  };
}

export interface Built {
  body: MailIntakeUpdate | null;
  errors: Record<string, string>;
}

/** The PATCH body of a form; the password is only sent when typed. */
export function buildUpdate(form: IntakeForm): Built {
  const errors: Record<string, string> = {};
  const port = Number(form.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    errors["port"] = "Enter a port from 1 to 65535.";
  }
  if (form.enabled && form.host.trim() === "") {
    errors["host"] = "Enter the mail server's host name.";
  }
  if (form.enabled && form.username.trim() === "") {
    errors["username"] = "Enter the mailbox's user name.";
  }
  for (const sender of form.senders) {
    if (!sender.address.includes("@") || sender.userId === "") {
      errors["senders"] = "Each extra sender needs an email address and a member.";
    }
  }
  if (Object.keys(errors).length > 0) {
    return { body: null, errors };
  }
  return {
    errors,
    body: {
      enabled: form.enabled,
      host: form.host.trim() === "" ? null : form.host.trim(),
      port,
      username: form.username.trim() === "" ? null : form.username.trim(),
      ...(form.password === "" ? {} : { password: form.password }),
      inbox_folder: form.inboxFolder.trim(),
      processed_folder: form.processedFolder.trim(),
      rejected_folder: form.rejectedFolder.trim(),
      require_auth_results: form.requireAuthResults,
      authserv_id: form.authservId.trim() === "" ? null : form.authservId.trim(),
      member_addresses: form.memberAddresses,
      allowed_senders: form.senders.map((s) => ({ address: s.address.trim(), user_id: s.userId })),
    },
  };
}

const OUTCOMES: Readonly<Record<MailTest["outcome"], string>> = {
  ok: "Connected and signed in.",
  not_configured: "The mailbox is not set up yet.",
  blocked: "This server address is not allowed (private and local addresses are refused).",
  connect_failed: "Could not reach the mail server.",
  tls_failed: "The server's certificate was not accepted.",
  auth_failed: "The mail server refused the user name or password.",
  folder_missing: "A folder named above does not exist on the mail server.",
  protocol_error: "The server answered in a way PerFin could not use.",
  timeout: "The mail server took too long to answer.",
};

export function describeTest(result: MailTest): string {
  const text = OUTCOMES[result.outcome];
  if (result.ok && typeof result.messages === "number") {
    const moves = result.move_supported === false ? " The server cannot move messages, so handled mail stays in the inbox." : "";
    return `${text} ${String(result.messages)} message(s) waiting.${moves}`;
  }
  return text;
}
