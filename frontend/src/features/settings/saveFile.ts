// SPDX-License-Identifier: AGPL-3.0-or-later
/** Hand a blob to the browser as a file download. Kept apart so tests can replace it. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

export function exportFilename(encrypted: boolean, now: Date = new Date()): string {
  const stamp = now.toISOString().slice(0, 10);
  return `perfin-export-${stamp}.${encrypted ? "pfarch" : "zip"}`;
}
