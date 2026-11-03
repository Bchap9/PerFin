// SPDX-License-Identifier: AGPL-3.0-or-later
/** Join class names, skipping falsy entries. */
export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter((name): name is string => typeof name === "string" && name !== "").join(" ");
}
