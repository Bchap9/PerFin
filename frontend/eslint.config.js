// SPDX-License-Identifier: AGPL-3.0-or-later
import js from "@eslint/js";
import nounsanitized from "eslint-plugin-no-unsanitized";
import reactHooks from "eslint-plugin-react-hooks";
import security from "eslint-plugin-security";
import globals from "globals";
import tseslint from "typescript-eslint";

const bannedSyntax = [
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "dangerouslySetInnerHTML is banned (SECURITY.md F1). Render text through React.",
  },
  {
    selector: "Property[key.name='dangerouslySetInnerHTML']",
    message: "dangerouslySetInnerHTML is banned (SECURITY.md F1). Render text through React.",
  },
  {
    selector: "MemberExpression[property.name=/^(innerHTML|outerHTML)$/]",
    message: "Direct HTML injection is banned (SECURITY.md F1).",
  },
  {
    selector: "CallExpression[callee.property.name='insertAdjacentHTML']",
    message: "Direct HTML injection is banned (SECURITY.md F1).",
  },
];

export default tseslint.config(
  { ignores: ["dist/", "coverage/", "node_modules/", "src/lib/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  security.configs.recommended,
  nounsanitized.configs.recommended,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-restricted-syntax": ["error", ...bannedSyntax],
      "no-restricted-globals": ["error", "eval"],
      "no-eval": "error",
      "no-implied-eval": "off",
      "@typescript-eslint/no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",
    },
  },
  {
    files: ["*.config.{js,ts}"],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ["**/*.js"],
    ...tseslint.configs.disableTypeChecked,
  },
);
