import js from "@eslint/js";
import tseslint from "typescript-eslint";

/**
 * Two environments, one repo: `src/` is TypeScript running in the extension
 * host, `media/` is plain JavaScript running in a webview with no Node and no
 * `vscode`. They need different globals, which is the other half of why the
 * webview code had to stop living inside template literals — as a string it
 * had no environment at all, and nothing could be said about it.
 */
export default [
  { ignores: ["out/**", "node_modules/**", "harness/**"] },

  // --- the extension host ------------------------------------------------
  js.configs.recommended,
  // scoped: these rules are for the host, and media/ is not TypeScript
  ...tseslint.configs.recommended.map((c) => ({
    ...c,
    files: ["src/**/*.ts"],
  })),
  {
    files: ["src/**/*.ts"],
    rules: {
      // A dropped promise in a command handler is a silent no-op, which is
      // the failure mode hardest to notice in this codebase.
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      eqeqeq: ["error", "smart"],
      "no-console": "error", // the output channel, not stdout
    },
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    // A test reports through stdout — that is what the runner reads, and
    // there is no output channel to send it to. The rule above is about the
    // extension, which has one.
    files: ["src/test/**/*.ts"],
    rules: { "no-console": "off" },
  },

  // --- the webviews ------------------------------------------------------
  {
    files: ["media/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        document: "readonly",
        window: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        requestAnimationFrame: "readonly",
        Option: "readonly",
        MutationObserver: "readonly",
        Plotly: "readonly",
        acquireVsCodeApi: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["error", { args: "none" }],
      eqeqeq: ["error", "smart"],
    },
  },
];
