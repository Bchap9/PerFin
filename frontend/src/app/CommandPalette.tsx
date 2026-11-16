// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The command palette: Ctrl+K or Cmd+K anywhere in the shell (spec section 8).
 * It goes to any section, section tab, settings page or account; it does not
 * search transactions.
 *
 * - The shortcut always needs Ctrl or Cmd, so typing "k" in a text field never
 *   opens it; with the modifier it opens from anywhere, and toggles it closed.
 * - A modal `Dialog` with the ARIA combobox pattern: the input owns the focus,
 *   Up and Down move the active option (`aria-activedescendant`), Enter goes,
 *   Escape closes and focus returns to where it was.
 * - Accounts come from the query cache (the list a page already loaded under
 *   `queryRoots.accounts`); the palette never fetches.
 * - Nothing is mounted while it is closed.
 */
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { cx } from "../components/cx";
import { Dialog } from "../components/Dialog";
import { accountKeys } from "../features/accounts/api";
import { useAuthSession } from "../features/auth/session";
import { accountCommands, rank, staticCommands, type Command, type PaletteAccount } from "./commands";
import "./styles/palette.css";

function isPaletteAccount(value: unknown): value is PaletteAccount {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = new Map<string, unknown>(Object.entries(value));
  const nickname = record.get("nickname");
  return (
    typeof record.get("id") === "string" &&
    typeof record.get("name") === "string" &&
    (nickname === null || nickname === undefined || typeof nickname === "string")
  );
}

/** The accounts already in the cache, or none. Never fetches. */
function cachedAccounts(queryClient: QueryClient): PaletteAccount[] {
  const data: unknown = queryClient.getQueryData(accountKeys.all);
  if (!Array.isArray(data)) {
    return [];
  }
  return data.filter(isPaletteAccount).map((account) => ({
    id: account.id,
    name: account.name,
    nickname: account.nickname ?? null,
  }));
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { session } = useAuthSession();
  const isInstanceAdmin = session?.user.is_instance_admin === true;
  const [accounts] = useState(() => cachedAccounts(queryClient));
  const commands = useMemo(
    () => [...staticCommands(isInstanceAdmin), ...accountCommands(accounts)],
    [accounts, isInstanceAdmin],
  );
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const results = useMemo(() => rank(commands, query), [commands, query]);
  const inputRef = useRef<HTMLInputElement>(null);
  const baseId = useId();
  const inputId = `${baseId}-input`;
  const listId = `${baseId}-list`;
  const optionId = (index: number) => `${baseId}-option-${String(index)}`;
  const activeIndex = results.length === 0 ? -1 : Math.min(active, results.length - 1);
  const expanded = results.length > 0;

  useEffect(() => {
    // After Dialog has noted where focus was, so closing returns it there.
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    if (activeIndex >= 0) {
      const option = document.getElementById(`${baseId}-option-${String(activeIndex)}`);
      // jsdom has no scrollIntoView.
      if (option !== null && "scrollIntoView" in option) {
        option.scrollIntoView({ block: "nearest" });
      }
    }
  }, [activeIndex, baseId]);

  function go(command: Command) {
    onClose();
    void navigate({
      to: command.to,
      ...(command.params === undefined ? {} : { params: command.params }),
      search: command.search ?? {},
    });
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (results.length === 0) {
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((activeIndex + 1) % results.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((activeIndex - 1 + results.length) % results.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const command = results.at(activeIndex);
      if (command !== undefined) {
        go(command);
      }
    }
  }

  return (
    <Dialog open onClose={onClose} title="Go to a page" closeLabel="Close the palette" className="palette">
      <label htmlFor={inputId} className="visually-hidden">
        Go to
      </label>
      <input
        ref={inputRef}
        id={inputId}
        className="palette__input"
        type="text"
        role="combobox"
        autoComplete="off"
        spellCheck={false}
        placeholder="A section, settings page or account"
        aria-autocomplete="list"
        aria-expanded={expanded}
        aria-controls={listId}
        aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <ul id={listId} role="listbox" aria-label="Pages and accounts" className="palette__list" hidden={!expanded}>
        {results.map((command, index) => (
          <li
            key={command.id}
            id={optionId(index)}
            role="option"
            aria-selected={index === activeIndex}
            className={cx("palette__option", index === activeIndex && "palette__option--active")}
            onMouseDown={(event) => {
              // Keep the focus in the input.
              event.preventDefault();
            }}
            onMouseMove={() => {
              if (index !== activeIndex) {
                setActive(index);
              }
            }}
            onClick={() => {
              go(command);
            }}
          >
            {command.label}
          </li>
        ))}
      </ul>
      <p role="status" className={cx("palette__empty", expanded && "visually-hidden")}>
        {expanded
          ? ""
          : accounts.length > 0
            ? "No page or account matches."
            : "No page matches. Accounts appear here once a page has loaded them."}
      </p>
      <p className="palette__hint">Up and Down to move, Enter to go, Escape to close.</p>
    </Dialog>
  );
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      // `code` too, so a layout where the K key types another letter still opens it.
      const isK = event.key.toLowerCase() === "k" || event.code === "KeyK";
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && isK) {
        event.preventDefault();
        setOpen((current) => !current);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  return open ? (
    <PaletteDialog
      onClose={() => {
        setOpen(false);
      }}
    />
  ) : null;
}
