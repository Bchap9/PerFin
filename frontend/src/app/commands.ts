// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the command palette (app/CommandPalette.tsx) can go to: every section,
 * section tab and settings page of the route map, and the accounts already
 * loaded. It navigates only; it never searches transactions (spec section 8).
 */
import { SECTIONS, SETTINGS_GROUPS } from "./routeMap";

export interface Command {
  /** Unique among the commands; also the option's DOM id suffix. */
  id: string;
  label: string;
  to: string;
  search?: Readonly<Record<string, string>>;
  params?: Readonly<Record<string, string>>;
}

export interface PaletteAccount {
  id: string;
  name: string;
  nickname: string | null;
}

/** At most this many results are shown. */
export const MAX_RESULTS = 12;

/** Sections, then their tabs, then Settings and its pages (instance pages only for an instance administrator). */
export function staticCommands(isInstanceAdmin: boolean): Command[] {
  const commands: Command[] = SECTIONS.map((section) => ({
    id: `section:${section.id}`,
    label: section.label,
    to: section.to,
  }));
  for (const section of SECTIONS) {
    section.tabs.forEach((tab, index) => {
      commands.push({
        id: `tab:${section.id}:${String(index)}`,
        label: `${section.label}: ${tab.label}`,
        to: tab.to,
        ...(tab.search === undefined ? {} : { search: tab.search }),
      });
    });
  }
  commands.push({ id: "settings", label: "Settings", to: "/settings" });
  for (const group of SETTINGS_GROUPS) {
    for (const page of group.pages) {
      if (page.instanceAdmin === true && !isInstanceAdmin) {
        continue;
      }
      commands.push({ id: `settings:${page.to}`, label: `Settings: ${page.label}`, to: page.to });
    }
  }
  return commands;
}

/** "Account: <name>" for each account, going to its page; the nickname wins when set. */
export function accountCommands(accounts: readonly PaletteAccount[]): Command[] {
  return accounts.map((account) => ({
    id: `account:${account.id}`,
    label: `Account: ${account.nickname !== null && account.nickname !== "" ? account.nickname : account.name}`,
    to: "/accounts/$accountId",
    params: { accountId: account.id },
  }));
}

/** Lower case, without accents ("Café" and "cafe" match). */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

function isWordChar(char: string | undefined): boolean {
  return char !== undefined && /[\p{L}\p{N}]/u.test(char);
}

/** 0: prefix, 1: a word starts with it, 2: anywhere inside, -1: no match. */
function tier(label: string, query: string): number {
  if (label.startsWith(query)) {
    return 0;
  }
  let index = label.indexOf(query, 1);
  if (index < 0) {
    return -1;
  }
  while (index >= 0) {
    if (!isWordChar(label.at(index - 1))) {
      return 1;
    }
    index = label.indexOf(query, index + 1);
  }
  return 2;
}

/**
 * The commands that match `query`, best first: a prefix match, then a match at
 * the start of a word, then anywhere inside. Case and accents are ignored; the
 * given order holds inside a tier. At most `MAX_RESULTS`; an empty query gives
 * the first ones.
 */
export function rank(commands: readonly Command[], query: string): Command[] {
  const wanted = fold(query.trim());
  if (wanted === "") {
    return commands.slice(0, MAX_RESULTS);
  }
  const tiers: Command[][] = [[], [], []];
  for (const command of commands) {
    const found = tier(fold(command.label), wanted);
    if (found >= 0) {
      tiers.at(found)?.push(command);
    }
  }
  return tiers.flat().slice(0, MAX_RESULTS);
}
