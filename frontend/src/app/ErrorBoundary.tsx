// SPDX-License-Identifier: AGPL-3.0-or-later
import { Component, type ErrorInfo, type ReactNode } from "react";

import { ErrorFallback } from "./ErrorFallback";

interface Props {
  children: ReactNode;
  /** Custom fallback; receives the error and a reset function. */
  fallback?: (error: unknown, reset: () => void) => ReactNode;
  onError?: (error: unknown, info: ErrorInfo) => void;
}

interface State {
  error: unknown;
  hasError: boolean;
}

/** Last-resort boundary around the whole app (routes have their own errorComponent). */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null, hasError: false };

  static getDerivedStateFromError(error: unknown): State {
    return { error, hasError: true };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    this.props.onError?.(error, info);
  }

  reset = (): void => {
    this.setState({ error: null, hasError: false });
  };

  override render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }
    if (this.props.fallback !== undefined) {
      return this.props.fallback(this.state.error, this.reset);
    }
    return (
      <main className="app-main" id="main">
        <ErrorFallback error={this.state.error} onRetry={this.reset} />
      </main>
    );
  }
}
