// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The actions of one grid row behind a single menu button, so a row stays
 * one line tall however many actions it has. The menu itself, its keyboard
 * and its placement are `components/Menu`.
 */
import { Menu, type MenuItem } from "../../components/Menu";

export type RowAction = MenuItem;

export function RowActions({ label, actions }: { label: string; actions: readonly RowAction[] }) {
  return <Menu label={`Actions for ${label}`} items={actions} />;
}
