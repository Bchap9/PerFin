// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Registers the service worker (`public/sw.js`, served from the site root so
 * its scope is the whole app). It is registered from this module, not an
 * inline script (CSP: `script-src 'self'`), and through a Trusted Types
 * policy because the CSP sets `require-trusted-types-for 'script'`.
 * Development (Vite) and browsers without service workers skip it.
 */
export const SERVICE_WORKER_URL = "/sw.js";

interface TrustedTypesPolicyFactory {
  createPolicy: (name: string, rules: { createScriptURL: (input: string) => string }) => { createScriptURL: (input: string) => string };
}

/** The URL as the browser wants it: a TrustedScriptURL where Trusted Types exist. */
export function scriptUrl(factory: TrustedTypesPolicyFactory | undefined): string {
  if (factory === undefined) {
    return SERVICE_WORKER_URL;
  }
  const policy = factory.createPolicy("perfin-service-worker", {
    createScriptURL: (input) => {
      if (input !== SERVICE_WORKER_URL) {
        throw new TypeError("only the PerFin service worker may be registered");
      }
      return input;
    },
  });
  return policy.createScriptURL(SERVICE_WORKER_URL);
}

export function registerServiceWorker(enabled: boolean = import.meta.env.PROD): void {
  if (!enabled || !("serviceWorker" in navigator)) {
    return;
  }
  const factory = (globalThis as { trustedTypes?: TrustedTypesPolicyFactory }).trustedTypes;
  const register = (): void => {
    navigator.serviceWorker.register(scriptUrl(factory), { scope: "/" }).catch(() => {
      // Installing is an enhancement; the app works the same without it.
    });
  };
  if (document.readyState === "complete") {
    register();
  } else {
    window.addEventListener("load", register, { once: true });
  }
}
