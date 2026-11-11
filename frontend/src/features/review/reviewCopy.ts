// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What each review item means and how to resolve it, keyed by its kind and
 * `reason` (the server's `payload.reason`). Pure data so it is testable and
 * every reason has words a person can act on.
 */
import { formatDate } from "../../lib/dates";
import { formatDecimal, formatMoney } from "../../lib/money";
import {
  canImportAsRead,
  hasSuggestedKinds,
  payloadDifferences,
  payloadIds,
  payloadLines,
  payloadNumber,
  payloadSections,
  payloadString,
  type Difference,
  type ReviewItem,
  type SectionDifference,
} from "./api";

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

/** An amount of the item's currency (a plain number when it names none). */
function money(amount: string, currency: string | null): string {
  const shown =
    currency === null
      ? formatDecimal(amount, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })
      : formatMoney(amount, currency);
  return shown ?? amount;
}

function unsigned(amount: string): string {
  return amount.trim().replace(/^[-+\u2212]/, "");
}

/** One failed brokerage check in plain words, with its difference. */
export function differenceText(
  difference: Difference,
  currency: string | null,
): string {
  const printed = money(difference.printed, currency);
  const computed = money(difference.computed, currency);
  const apart = `a difference of ${money(unsigned(difference.difference), currency)}`;
  switch (difference.check) {
    case "positions": {
      const count = difference.count ?? 1;
      const which = count === 1 ? "1 holding" : `${String(count)} holdings`;
      return `Positions: for ${which}, quantity × price comes to ${computed}, but the statement prints a market value of ${printed} (${apart}).`;
    }
    case "holdings_total":
      return `Holdings total: the holdings read add up to ${computed}, but the statement prints a total of ${printed} (${apart}).`;
    case "cash":
      return `Cash: the opening cash plus this period's activity comes to ${computed}, but the statement prints a closing cash of ${printed} (${apart}).`;
    case "total_value":
      return `Account value: the holdings plus the cash come to ${computed}, but the statement prints an account value of ${printed} (${apart}).`;
    default:
      return `${difference.check}: ${computed} read against ${printed} printed (${apart}).`;
  }
}

/** What did not add up on a held brokerage statement, one sentence per check. */
export function differenceTexts(item: ReviewItem): string[] {
  const currency = payloadString(item, "currency");
  return payloadDifferences(item).map((d) => differenceText(d, currency));
}

/** One section whose rows are not its printed total, in plain words. */
export function sectionText(
  section: SectionDifference,
  currency: string | null,
): string {
  const lines = section.rows === 1 ? "1 line" : `${String(section.rows)} lines`;
  const where =
    section.source === "summary" ? " in its cash-flow summary" : "";
  return `${section.label}: the statement prints ${money(section.printed, currency)}${where}; the ${lines} read add up to ${money(section.computed, currency)} (a difference of ${money(unsigned(section.difference), currency)}).`;
}

/** The sections that do not add up on a brokerage statement, one sentence each. */
export function sectionTexts(item: ReviewItem): string[] {
  const currency = payloadString(item, "currency");
  return payloadSections(item).map((s) => sectionText(s, currency));
}

/** Cash-flow lines of the statement's summary no line was read for. */
function unreadText(item: ReviewItem): string {
  const labels = payloadIds(item, "unread_summary_lines");
  return labels.length === 0
    ? ""
    : ` The statement's cash-flow summary also lists ${labels.join(", ")}, for which no lines were read.`;
}

/** "3 lines" / "1 line" of the item's lines in doubt, or null when it lists none. */
function doubtedLines(item: ReviewItem): string | null {
  const count = payloadNumber(item, "held") ?? payloadLines(item).length;
  if (count === 0) {
    return null;
  }
  return count === 1 ? "1 line" : `${String(count)} lines`;
}

const SUGGESTED_WHY =
  "The local model only suggests; PerFin never records a kind of activity on a model's word, so it asks you to confirm.";

const AS_READ_STEP =
  "“Import as read” if the statement itself is off (rounding, items it leaves out): its trades, holdings and prices are imported as printed, but no account value is recorded from this statement.";

/** The "import as read" step, when the item offers it (``canImportAsRead``). */
function asReadSteps(item: ReviewItem, step: string): string[] {
  return canImportAsRead(item) ? [step] : [];
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
  "possible_duplicate:same_content": () => ({
    title: "Uploaded before",
    what: "A file with exactly the same content is already in PerFin.",
    steps: [
      "“Reject this upload” if it is the same file.",
      "“Not a duplicate” to import it anyway.",
    ],
  }),
  "possible_duplicate:near_match": () => ({
    title: "Possible duplicate row",
    what: "A row of this file has the same date and amount as a transaction already recorded.",
    steps: [
      "Compare the two rows.",
      "“It is a duplicate” removes the new one; “Not a duplicate” keeps both.",
    ],
  }),
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
    what: "The file was read, but PerFin found no transactions or statements in it.",
    why: "The layout may be one PerFin does not know yet.",
    steps: [
      "If it is a statement, use “Teach PerFin”: select the period, account number, balances and the transaction table on the page. Tick “Remember this layout” so the next file is read on its own.",
      "Otherwise check that you exported the right date range and upload again, or dismiss this item.",
    ],
  }),
  "unparsed:loan_unmatched": () => ({
    title: "Which loan?",
    what: "This loan statement does not match any loan recorded in PerFin.",
    steps: [
      "Create the loan (Loans, New loan) with the account number's last four digits, then “Process again”.",
      "Or dismiss this item if you do not track this loan.",
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
        "The skipped lines are highlighted on the page where PerFin knows them.",
        "Add any that matter by hand (Investments or Transactions), or use “Teach PerFin” to show it the table and its columns.",
        "Dismiss this item once you are done.",
      ],
    };
  },
  "low_confidence_field:rows_skipped": (item) => ({
    title: "Some rows were skipped",
    what: `${count(item, "count")} rows of this file could not be read (a date or amount PerFin could not make out) and were not imported.`,
    steps: [
      "Open “Map columns” and check the date format and the amount columns, or add the rows by hand.",
      "Dismiss this item once you are done.",
    ],
  }),
  "low_confidence_field:balance_mismatch": (item) => ({
    title: "Running balance does not match",
    what: `On ${count(item, "count")} rows the running balance the file prints does not follow from the amounts.`,
    why: "A row may be missing or misread, so the rows are held as pending.",
    steps: [
      "Compare the rows with the file and correct the wrong ones.",
      "Confirm when they are right.",
    ],
  }),
  "low_confidence_field:date_order_assumed": () => ({
    title: "Month or day first?",
    what: "Every date in this file fits both month-first and day-first, so PerFin assumed month-first.",
    steps: [
      "Check a few dates against the file.",
      "If they are wrong, open “Map columns”, choose the right date format and save; otherwise confirm.",
    ],
  }),
  "low_confidence_field:truncated": () => ({
    title: "File cut short",
    what: "This file is larger than PerFin reads in one go, so only its first part was imported.",
    steps: [
      "Export a shorter date range and upload it in parts.",
      "Dismiss this item once the rest is in.",
    ],
  }),
  "low_confidence_field:currency_mismatch": (item) => ({
    title: "Rows in another currency",
    what: `${count(item, "count")} rows are in a different currency from the account and were not imported.`,
    steps: [
      "Import them into an account in that currency, or add them by hand.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:payslip_does_not_add_up": () => ({
    title: "Payslip does not add up",
    what: "The gross pay less the deductions read from this payslip is not its net pay.",
    steps: [
      "Compare the amounts with the payslip and correct them on the document.",
      "Dismiss this item once it is right.",
    ],
  }),
  "low_confidence_field:tax_form_low_confidence": () => ({
    title: "Check the tax form",
    what: "Some boxes of this tax form were read with low confidence.",
    steps: [
      "Compare the amounts in Tax with the form and correct them there.",
      "Dismiss this item once they are right.",
    ],
  }),
  "low_confidence_field:tax_form_unread": () => ({
    title: "Tax form not read",
    what: "This looks like a tax form, but PerFin could not read its boxes.",
    steps: [
      "Enter the amounts in Tax by hand.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:loan_low_confidence": () => ({
    title: "Check the loan statement",
    what: "Some figures of this loan statement were read with low confidence, so the payment was not recorded.",
    steps: [
      "Compare the statement with the loan's payments and record the payment by hand if needed.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:loan_payment_out_of_range": () => ({
    title: "Unusual loan payment",
    what: "The payment on this loan statement is far from the loan's usual payment, so it was not recorded.",
    steps: [
      "Check the statement; record the payment by hand if it is right.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:loan_reconciliation_failed": () => ({
    title: "Loan statement does not add up",
    what: "Principal, interest and escrow read from this statement do not add up to its payment or balance.",
    steps: [
      "Compare the statement with the loan and record the payment by hand.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:loan_currency_mismatch": () => ({
    title: "Loan in another currency",
    what: "This loan statement is in a different currency from the loan it matched.",
    steps: ["Check which loan it belongs to.", "Dismiss this item."],
  }),
  "low_confidence_field:loan_rate_differs": () => ({
    title: "Interest rate changed?",
    what: "The rate on this statement differs from the rate recorded for the loan.",
    steps: [
      "If the rate changed, update it on the loan.",
      "Dismiss this item.",
    ],
  }),
  "low_confidence_field:investment_date_out_of_range": () => ({
    title: "Dates outside the statement",
    what: "Some activity of this brokerage statement is dated outside its period, so it was held back.",
    steps: [
      "Check the dates against the statement; use “Teach PerFin” to show PerFin the statement period.",
      "Process the file again once it is right.",
    ],
  }),
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
      "If PerFin misread a balance, enter the balances the statement prints and “Check and accept”.",
      "Otherwise compare the rows with the document and correct the wrong ones.",
      "Confirm when the totals match.",
    ],
  }),
  "low_confidence_field:reconciliation_unavailable": () => ({
    title: "Could not check the totals",
    what: "PerFin found no balances on this statement to check its rows against, so they are pending.",
    steps: [
      "If the statement prints its opening and closing balance, enter them and “Check and accept”.",
      "Or glance over the rows and confirm them.",
    ],
  }),
  "low_confidence_field:investment_opening_basis_unknown": () => ({
    title: "Cost basis needed",
    what: "This export sells shares that were bought before its first row. PerFin added them as an opening position with an unknown cost basis (zero for now).",
    why: "Gains on these sales are overstated until the real cost is entered.",
    steps: [
      "Enter the cost per unit of each opening position below (or on the opening transaction in Investments).",
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
  "low_confidence_field:investment_same_name_securities": () => ({
    title: "Two holdings share a name",
    what: "This statement lists holdings under the same name at different prices, with no ticker or CUSIP to tell them apart, so nothing of it was imported.",
    steps: [
      "Compare the holdings on the statement: a name may continue on a second line that tells them apart.",
      "Use “Teach PerFin” to show the holdings table, or add these holdings by hand in Investments, then dismiss this item.",
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
  "low_confidence_field:investment_low_confidence": (item) => {
    const fields = payloadIds(item, "fields");
    if (hasSuggestedKinds(item) && canImportAsRead(item)) {
      const lines = doubtedLines(item) ?? "some lines";
      return {
        title: "Check what the local model suggested",
        what: `PerFin did not know the wording of ${lines} of this statement, so the local model suggested what kind of activity ${lines === "1 line" ? "it is" : "they are"} (listed below). ${lines === "1 line" ? "It moves" : "They move"} shares, which the holdings are checked against, so nothing of the statement was imported yet.`,
        why: SUGGESTED_WHY,
        steps: [
          "Compare the suggested kind of each line below with the statement (“Show document”).",
          "“Import as read” if the suggestions are right: the statement is imported with them.",
          "“Dismiss” to import nothing from it; add its activity by hand.",
        ],
      };
    }
    if (fields.length === 1 && fields[0] === "reconciliation") {
      return {
        title: "Nothing to check against",
        what: "This statement prints no totals PerFin could check its holdings and cash against, so it was held back.",
        why: "PerFin checks every statement's rows against its printed totals before importing it.",
        steps: [
          "Compare what was read with the statement (“Show document”).",
          ...asReadSteps(
            item,
            "“Import as read” imports its trades, holdings and prices as printed; no account value is recorded from this statement.",
          ),
          "“Teach PerFin” to show where its totals are.",
          "“Dismiss” to import nothing from it.",
        ],
      };
    }
    return {
      title: "Dates may be wrong",
      what: "PerFin could not tell the day from the month in this file's dates, so the statement was held back.",
      steps: [
        "Open “Map columns”, choose the right date format, and save.",
        "For a PDF, use “Teach PerFin” to show the statement period and the table.",
      ],
    };
  },
  "low_confidence_field:investment_lines_held": (item) => {
    const lines = doubtedLines(item) ?? "Some lines";
    const imported = payloadNumber(item, "imported");
    const rest =
      imported === null || imported === 0
        ? "The holdings were imported"
        : `The holdings and the other ${String(imported)} activity lines were imported`;
    const sections = sectionTexts(item);
    const one = lines === "1 line";
    const fields = payloadIds(item, "fields");
    if (hasSuggestedKinds(item) && !fields.includes("reconciliation")) {
      return {
        title: "Check what the local model suggested",
        what: `${rest}; PerFin did not know the wording of ${lines.toLowerCase()} of this statement, so the local model suggested what kind of activity ${one ? "it is" : "they are"} (listed below) and ${one ? "it waits" : "they wait"} for you.`,
        why: SUGGESTED_WHY,
        steps: [
          "Compare the suggested kind of each line below with the statement (“Show document”).",
          ...asReadSteps(
            item,
            "“Import as read” if the suggestions are right: these lines are imported with them.",
          ),
          "“Dismiss” to leave these lines out; add any that are right by hand.",
        ],
      };
    }
    return {
      title: "A few lines need a look",
      what: `${rest}; ${lines} of this statement ${one ? "was" : "were"} held back because the statement's own totals do not vouch for ${one ? "it" : "them"}.${sections.length === 0 ? "" : ` ${sections.join(" ")}`}${unreadText(item)}`,
      why: "PerFin checks each section's lines against the total the statement prints for it, and only holds the lines of a section that does not add up.",
      steps: [
        "Compare the lines listed below with the statement (“Show document”).",
        ...asReadSteps(
          item,
          "“Import as read” imports these lines as printed.",
        ),
        "“Teach PerFin” if a table or a figure was read wrong.",
        "“Dismiss” to leave these lines out; add any that are right by hand.",
      ],
    };
  },
  "low_confidence_field:investment_figures_differ": (item) => {
    const texts = [...differenceTexts(item), ...sectionTexts(item)];
    return {
      title: "Statement imported; its totals disagree",
      what: `Every line of this statement is vouched for by its own totals, so it was imported.${texts.length === 0 ? " Some of its printed totals disagree with each other." : ` ${texts.join(" ")}`}${unreadText(item)}`,
      why: "Nothing read from the statement is in doubt, but PerFin does not record an account value from totals that disagree.",
      steps: [
        "Compare the figures with the statement (“Show document”).",
        "“Dismiss” once you have seen it; enter the account's value by hand if you want it recorded.",
        "“Teach PerFin” if a figure was read wrong.",
      ],
    };
  },
  "low_confidence_field:investment_reconciliation_failed": (item) => {
    const texts = [...differenceTexts(item), ...sectionTexts(item)];
    const lines = doubtedLines(item);
    const where =
      lines === null
        ? ""
        : ` ${lines === "1 line" ? "The 1 line" : `The ${lines}`} in doubt ${lines === "1 line" ? "is" : "are"} listed below.`;
    return {
      title: "Statement does not add up",
      what:
        texts.length === 0
          ? `The figures read from this statement do not add up to its totals, so nothing of it was imported.${where}`
          : `The figures read from this statement do not add up, so nothing of it was imported. ${texts.join(" ")}${unreadText(item)}${where}`,
      why: "A statement can round or leave items out, but a misread table looks the same, so PerFin asks before importing it.",
      steps: [
        "Compare the figures with the statement (“Show document”).",
        ...asReadSteps(item, AS_READ_STEP),
        "“Teach PerFin” if a table or a figure was read wrong.",
        "“Dismiss” to import nothing from it.",
      ],
    };
  },
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
