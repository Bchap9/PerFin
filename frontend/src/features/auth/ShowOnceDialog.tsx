// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Shows a secret the server returns exactly once (recovery codes, a personal
 * access token). It cannot be shown again, so the dialog only closes after
 * the user confirms they stored it. The secret lives in React state only and
 * is dropped when the dialog closes.
 */
import { useState, type ReactNode } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";

export interface ShowOnceDialogProps {
  open: boolean;
  title: string;
  description: string;
  /** One or more lines (codes, a token). */
  lines: readonly string[];
  /** Label of the copy button and its list. */
  label: string;
  onDone: () => void;
  /** Extra content under the secret (for example setup instructions). */
  children?: ReactNode;
}

export function ShowOnceDialog({ open, title, description, lines, label, onDone, children }: ShowOnceDialogProps) {
  const [saved, setSaved] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);

  const finish = (): void => {
    if (!saved) {
      setCopyStatus("Confirm that you stored it before closing.");
      return;
    }
    setSaved(false);
    setCopyStatus(null);
    onDone();
  };

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopyStatus("Copied to the clipboard.");
    } catch {
      setCopyStatus("Copying is not available here. Select the text and copy it.");
    }
  };

  return (
    <Dialog
      open={open}
      onClose={finish}
      title={title}
      description={description}
      footer={
        <>
          <Button
            onClick={() => {
              void copy();
            }}
          >
            Copy {label}
          </Button>
          <Button variant="primary" disabled={!saved} onClick={finish}>
            Done
          </Button>
        </>
      }
    >
      <ul className="secret-list" aria-label={label}>
        {lines.map((line) => (
          <li key={line}>
            <code className="secret-value">{line}</code>
          </li>
        ))}
      </ul>
      {children}
      <Checkbox
        label="I have stored this somewhere safe. It will not be shown again."
        checked={saved}
        onChange={(event) => {
          setSaved(event.target.checked);
        }}
      />
      <p className="auth-muted" role="status" aria-live="polite">
        {copyStatus}
      </p>
    </Dialog>
  );
}
