// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Toast notifications. `ToastProvider` owns a polite live region (errors use
 * an assertive one) and `useToast().notify(...)` adds a message that
 * dismisses itself after `durationMs` (errors stay until dismissed).
 * Messages are plain text rendered by React.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { cx } from "./cx";
import { IconButton } from "./IconButton";
import { CloseIcon } from "./icons";

export type ToastTone = "info" | "success" | "warning" | "error";

export interface ToastInput {
  title: string;
  description?: string;
  tone?: ToastTone;
  /** Milliseconds before auto-dismiss; `null` keeps it until dismissed. */
  durationMs?: number | null;
}

export interface ToastItem {
  id: number;
  title: string;
  description: string | undefined;
  tone: ToastTone;
  durationMs: number | null;
}

interface ToastContextValue {
  notify: (toast: ToastInput) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);
const DEFAULT_DURATION_MS = 5_000;
const MAX_TOASTS = 5;

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (value === null) {
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return value;
}

function ToastView({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: number) => void }) {
  useEffect(() => {
    if (toast.durationMs === null) {
      return undefined;
    }
    const timer = setTimeout(() => {
      onDismiss(toast.id);
    }, toast.durationMs);
    return () => {
      clearTimeout(timer);
    };
  }, [toast.id, toast.durationMs, onDismiss]);

  return (
    <li className={cx("toast", `toast--${toast.tone}`)}>
      <div className="toast__content">
        <p className="toast__title">{toast.title}</p>
        {toast.description === undefined ? null : <p className="toast__description">{toast.description}</p>}
      </div>
      <IconButton
        label="Dismiss notification"
        icon={<CloseIcon />}
        size="sm"
        onClick={() => {
          onDismiss(toast.id);
        }}
      />
    </li>
  );
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback((input: ToastInput) => {
    const id = nextId.current;
    nextId.current += 1;
    const tone = input.tone ?? "info";
    const item: ToastItem = {
      id,
      title: input.title,
      description: input.description,
      tone,
      durationMs: input.durationMs === undefined ? (tone === "error" ? null : DEFAULT_DURATION_MS) : input.durationMs,
    };
    setToasts((current) => [...current, item].slice(-MAX_TOASTS));
    return id;
  }, []);

  const value = useMemo(() => ({ notify, dismiss }), [notify, dismiss]);
  const polite = toasts.filter((toast) => toast.tone !== "error");
  const assertive = toasts.filter((toast) => toast.tone === "error");

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-region" role="region" aria-label="Notifications">
        <ul className="toast-list" aria-live="polite" aria-relevant="additions text">
          {polite.map((toast) => (
            <ToastView key={toast.id} toast={toast} onDismiss={dismiss} />
          ))}
        </ul>
        <ul className="toast-list" role="alert" aria-live="assertive">
          {assertive.map((toast) => (
            <ToastView key={toast.id} toast={toast} onDismiss={dismiss} />
          ))}
        </ul>
      </div>
    </ToastContext.Provider>
  );
}
