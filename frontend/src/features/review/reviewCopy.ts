// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What each review item means and how to resolve it, keyed by its kind and
 * `reason` (the server's `payload.reason`). Pure data so it is testable and
 * every reason has words a person can act on.
 */
import { formatDate } from "../../lib/dates";
import { payloadNumber, payloadString, type ReviewItem } from "./api";

export interface Explanation {
  /** Short heading for the card. */
  title: string;
  /** What happened, in one or two sentences. */
  what: string;
  /** Why PerFin stopped instead of guessing (optional). */
  why?: string;
  /** Concrete steps, in order. */
  steps: string[];
}

const KIND_TITLES: Readonly<Record<ReviewItem["kind"], string>> = {
  unknown_owner: "Whose file is this?",
  unknown_account: "Which account?",
  low_confidence_field: "Check what was read",
  possible_duplicate: "Possible duplicate",
  unparsed: "Could not be read",
  new_institution: "New institution",
  rejected_email: "Rejected email",
  receipt_match: "Attach this receipt",
};

type Copy = (
  item: ReviewItem,
) => Omit<Explanation, "title"> & { title?: string };

function count(item: ReviewItem, key: string): string {
  return String(payloadNumber(item, key) ?? 0);
}

/** "Jan 1, 2026 – Jan 31, 2026" from two ISO dates of the payload (null if missing). */
export function payloadPeriod(
  item: ReviewItem,
  startKey: string,
  endKey: string,
): string | null {
  const start = payloadString(item, startKey);
  const end = payloadString(item, endKey);
  if (start === null || end === null) {
    return null;
  }
  return `${formatDate(start) ?? start} – ${formatDate(end) ?? end}`;
}

const ACCOUNT_STEPS = [
  "Choose the account below and import.",
  "If the account does not exist in PerFin yet, create it (Accounts, New account) and come back, or use “Set up accounts from this file”.",
  "Tick “Remember” so the next file with the same account number is imported on its own.",
];

const COPY: Readonly<Record<string, Copy>> = {
  // -- unknown_account -------------------------------------------------------
  "unknown_account:account_not_found": (item) => {
    const rows =
      payloadNumber(item, "transactions") ?? payloadNumber(item, "activity");
    const several =
      payloadNumber(item, "group") !== null ||
      item.payload["investment"] === true;
    return {
      what: several
        ? rows !== null && rows > 0
          ? `This file lists several accounts; ${String(rows)} of its rows belong to an account PerFin does not know.`
          : "This file lists several accounts; one of them is an account PerFin does not know."
        : "None of your accounts matches the account number in this file.",
      why: "PerFin matches files to accounts by the last four digits of the account number. No account of yours records these digits yet, or the file prints none.",
      steps: ACCOUNT_STEPS,
    };
  },
  "unknown_account:account_ambiguous": () => ({
    title: "Which of these accounts?",
    what: "Several of your accounts end in the same four digits as this file.",
    why: "PerFin will not pick between them on its own.",
    steps: [
      "Choose the right account below.",
      "Tick “Remember” so the choice is kept for next time.",
    ],
  }),
  "unknown_account:account_required": () => ({
    what: "This file has no account-number column, so PerFin cannot tell which account its rows belong to.",
    steps: [
      "Choose the account below and import.",
      "Next time, upload the file with an account chosen in the upload form.",
    ],
  }),
  "unknown_account:currency_mismatch": (item) => ({
    title: "Different currency",
    what: `The file is in ${payloadString(item, "currency") ?? "another currency"}, but the matched account is in ${payloadString(item, "account_currency") ?? "a different one"}.`,
    why: "Rows are never converted silently between currencies.",
    steps: [
      "Choose an account in the file's currency, or create one.",
      "If the matched account is wrong, choose the right one below.",
    ],
  }),
  "unknown_account:account_type_mismatch": (item) => ({
    title: "Wrong kind of account",
    what: `This is a brokerage statement, but the matched account is a ${payloadString(item, "account_type") ?? "non-investment"} account.`,
    why: "Trades and holdings can only go into brokerage or retirement accounts.",
    steps: ["Choose a brokerage or retirement account below, or create one."],
  }),
  // -- possible_duplicate ----------------------------------------------------
  "possible_duplicate:statement_overlap": (item) => {
    const period = payloadPeriod(item, "period_start", "period_end");
    const other = payloadPeriod(item, "other_period_start", "other_period_end");
    return {
      title: "Statement already imported?",
      what: `This file's statement${period === null ? "" : ` (${period})`} covers the same time as a statement already imported for this account from another file${other === null ? "" : ` (${other})`}. Nothing of it was imported yet.`,
      why: "The same statement in two files (a PDF and an OFX, or one uploaded twice) would add its rows twice, so PerFin asks first.",
      steps: [
        "Open the other file to compare.",
        "“Same statement — skip” if it is the same statement: nothing more is imported.",
        "“Import anyway” if it is a different statement: rows already recorded are still skipped.",
      ],
    };
  },
  // -- unparsed --------------------------------------------------------------
  "unparsed:mapping_required": () => ({
    title: "Needs a column mapping",
    what: "PerFin has not seen this spreadsheet layout before and could not tell its columns apart with confidence.",
    why: "A wrong guess would import wrong amounts or dates, so you confirm it once; the mapping is saved for every later file with the same header.",
    steps: [
      "Open “Map columns” and check the suggested role of each column.",
      "For a brokerage activity export, choose “Investment activity”.",
      "Save; the file is imported right away.",
    ],
  }),
  "unparsed:mapping_ambiguous": () => ({
    title: "Several mappings fit",
    what: "More than one saved column mapping fits this file, and they point at different accounts.",
    steps: [
      "Open “Map columns” and save the right mapping, or choose the account below.",
    ],
  }),
  "unparsed:mapping_failed": () => ({
    title: "The mapping did not read",
    what: "A saved column mapping fits this file's header, but no row could be read with it.",
    why: "The export may have changed its date or number format.",
    steps: ["Open “Map columns”, fix the date format or columns, and save."],
  }),
  "unparsed:activity_unreadable": (item) => ({
    title: "No activity could be read",
    what: `This looks like a brokerage activity export, but none of its rows could be read (${count(item, "unknown_activity")} with an action PerFin does not know, ${count(item, "skipped_rows")} unreadable).`,
    steps: [
      "Open “Map columns”, choose “Investment activity” and check the columns.",
      "If the actions are unusual, dismiss this and report the action words.",
    ],
  }),
  "unparsed:extraction_failed": () => ({
    what: "PerFin could not read this file.",
    why: "It may be damaged, an unusual format, or too large for the safe reader.",
    steps: [
      "Try “Process again”.",
      "If it fails again, download a fresh copy from your bank (CSV, OFX or PDF) and upload that.",
    ],
  }),
  "unparsed:format_not_supported": () => ({
    title: "Format not supported",
    what: "PerFin cannot import this kind of file.",
    steps: [
      "Download the statement as CSV, OFX/QFX, QIF, XLSX or PDF and upload that instead.",
      "Dismiss this item.",
    ],
  }),
  "unparsed:encrypted": () => ({
    title: "Password protected",
    what: "This PDF is password protected, so PerFin cannot read it.",
    steps: [
      "Save an unprotected copy (print to PDF) and upload that.",
      "Dismiss this item.",
    ],
  }),
  "unparsed:bad_password": () => ({
    title: "Password protected",
    what: "This file is password protected and could not be opened.",
    steps: ["Upload an unprotected copy, then dismiss this item."],
  }),
  "unparsed:no_records": () => ({
    title: "Nothing to import",
    what: "The file was read, but it holds no transactions or statements.",
    steps: [
      "Check that you exported the right date range, then upload again.",
      "Dismiss this item.",
    ],
  }),
  "unparsed:scan_failed": () => ({
    title: "Virus scan did not run",
    what: "The virus scanner could not check this file, so it was not processed.",
    steps: ["Try “Process again” once the scanner is back."],
  }),
  "unparsed:container_incomplete": () => ({
    title: "Some contents were left out",
    what: "This archive or email had contents PerFin refused or could not reach.",
    steps: ["Upload the left-out files on their own.", "Dismiss this item."],
  }),
  "unparsed:depth_limit": () => ({
    title: "Nested too deep",
    what: "This archive holds archives inside archives deeper than PerFin opens.",
    steps: ["Unpack it and upload the statements themselves."],
  }),
  "unparsed:import_pending": () => ({
    what: "This file is waiting to be imported.",
    steps: ["Try “Process again”."],
  }),
  // -- low_confidence_field --------------------------------------------------
  "low_confidence_field:lines_skipped": (item) => {
    const examples = Array.isArray(item.payload["unknown_actions"])
      ? (item.payload["unknown_actions"] as unknown[])
          .map((entry) =>
            entry !== null && typeof entry === "object" && "action" in entry
              ? String(entry.action)
              : "",
          )
          .filter((text) => text !== "")
      : [];
    return {
      title: "Some rows were skipped",
      what: `${count(item, "count")} rows of this file were not imported${payloadNumber(item, "unknown_activity") ? `, ${count(item, "unknown_activity")} of them with an action PerFin does not know` : ""}.${examples.length > 0 ? ` Unknown actions: ${examples.join(", ")}.` : ""}`,
      why: "PerFin never guesses what an unknown row means; everything else in the file was imported.",
      steps: [
        "Look at the skipped rows in the file and add any that matter by hand (Investments or Transactions).",
        "Dismiss this item once you are done.",
      ],
    };
  },
  "low_confidence_field:low_confidence": () => ({
    title: "Check what was read",
    what: "Some values were read with low confidence and are held as pending.",
    steps: [
      "Compare the highlighted values with the document.",
      "Correct anything wrong, then confirm.",
    ],
  }),
  "low_confidence_field:reconciliation_failed": () => ({
    title: "The statement does not add up",
    what: "The rows read from this statement do not add up to its opening and closing balance.",
    why: "A row may have been misread or missed, so every row is pending and no balance was recorded.",
    steps: [
      "Compare the rows with the document and correct the wrong ones.",
      "Confirm when the totals match.",
    ],
  }),
  "low_confidence_field:reconciliation_unavailable": () => ({
    title: "Could not check the totals",
    what: "This statement prints no balances to check its rows against, so they are pending.",
    steps: ["Glance over the rows and confirm them."],
  }),
  "low_confidence_field:investment_opening_basis_unknown": () => ({
    title: "Cost basis needed",
    what: "This export sells shares that were bought before its first row. PerFin added them as an opening position with an unknown cost basis (zero for now).",
    why: "Gains on these sales are overstated until the real cost is entered.",
    steps: [
      "Enter the cost basis on the opening transaction (Investments, the account, the “Opening position before this export” row).",
      "Or import an older export or statement for this account first.",
      "Dismiss this item once the basis is right.",
    ],
  }),
  "low_confidence_field:investment_lot_conflict": () => ({
    title: "Holdings do not line up",
    what: "This statement sells more shares than the account held, so nothing of it was imported.",
    steps: [
      "Import older statements for this account first, oldest first, then process this file again.",
    ],
  }),
  "low_confidence_field:investment_out_of_order": () => ({
    title: "Older than what is recorded",
    what: "This statement is older than activity already recorded for the account, so its new rows were not added.",
    steps: [
      "Add any missing rows by hand, or remove the newer imports and import oldest first.",
    ],
  }),
  "low_confidence_field:investment_possible_duplicate": (item) => ({
    title: "Trades already recorded",
    what: `${count(item, "rows")} trades in this file match trades already recorded under another date (for example a CSV dated by trade date and a PDF by settlement date).`,
    steps: [
      "Nothing to do if they are the same trades; dismiss this item.",
      "If a trade really happened twice, add it by hand.",
    ],
  }),
  "low_confidence_field:investment_low_confidence": () => ({
    title: "Dates may be wrong",
    what: "PerFin could not tell the day from the month in this file's dates, so the statement was held back.",
    steps: ["Open “Map columns”, choose the right date format, and save."],
  }),
  "low_confidence_field:investment_reconciliation_failed": () => ({
    title: "Holdings do not add up",
    what: "The positions read from this statement do not add up to its totals, so it was held back.",
    steps: [
      "Process the file again; if it still fails, enter the activity by hand.",
    ],
  }),
  "low_confidence_field:position_mismatch": () => ({
    title: "Holdings differ",
    what: "The statement reports fewer shares than the account's history adds up to.",
    steps: [
      "Check for a missing sale or transfer in the account's activity and add it.",
    ],
  }),
};

/** The explanation of an item (falls back to the kind's own words). */
export function explain(item: ReviewItem): Explanation {
  const reason = item.reason ?? payloadString(item, "reason");
  const found = reason === null ? undefined : COPY[`${item.kind}:${reason}`];
  const title = KIND_TITLES[item.kind];
  if (found !== undefined) {
    const copy = found(item);
    const { title: own, ...rest } = copy;
    return { ...rest, title: own ?? title };
  }
  return { title, ...FALLBACK[item.kind](item) };
}

const FALLBACK: Readonly<
  Record<ReviewItem["kind"], (item: ReviewItem) => Omit<Explanation, "title">>
> = {
  unknown_owner: () => ({
    what: "This file arrived in the drop folder without a member or account PerFin could recognise.",
    steps: [
      "Choose who it belongs to (and its account).",
      "Tick “Remember” to route that folder from now on.",
    ],
  }),
  unknown_account: () => ({
    what: "PerFin could not tell which account this file belongs to.",
    steps: ACCOUNT_STEPS,
  }),
  low_confidence_field: () => ({
    what: "Some values were read with low confidence.",
    steps: [
      "Compare them with the document, correct what is wrong, then confirm.",
    ],
  }),
  possible_duplicate: (item) => ({
    what:
      payloadString(item, "transaction_id") === null
        ? "This file has the same content as one already uploaded."
        : "A row of this file looks like a transaction already recorded.",
    steps: ["Compare them; confirm the duplicate or keep both."],
  }),
  unparsed: (item) =>
    item.next_step === "column_mapping"
      ? {
          what: "This file needs a column mapping before its rows can be imported.",
          steps: ["Open “Map columns” and save."],
        }
      : {
          what: "This file could not be read.",
          steps: [
            "Try “Process again”.",
            "Dismiss it if it is not a statement.",
          ],
        },
  new_institution: () => ({
    what: "This document's layout is not one PerFin knows yet.",
    steps: [
      "Choose its institution and “Remember this layout”, so similar files are read on their own.",
    ],
  }),
  rejected_email: () => ({
    what: "This email was not accepted.",
    steps: ["It can only be dismissed."],
  }),
  receipt_match: () => ({
    what: "This receipt has not been attached to a transaction yet.",
    steps: ["Choose the transaction it belongs to."],
  }),
};
